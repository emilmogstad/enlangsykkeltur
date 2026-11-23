"""GPX parsing, route lookup and export helpers."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import hashlib
import math
import os
import sys
import threading

import gpxpy
import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .data_handler import ROAD_COLUMNS, data_handler
from .nvdb_data import NVDB_COLUMNS, lookup_route_data, normalize_municipality_number

ARCHIVE = Path(__file__).resolve().parent.parent.parent / "Old application app"
sys.path.insert(0, str(ARCHIVE / "tools/src"))
_old_root = os.environ.pop("BIKINGMAN_ROOT", None)
try:
    from bikingman import lookup, viewer
finally:
    if _old_root is not None:
        os.environ["BIKINGMAN_ROOT"] = _old_root

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="race-lookup")
_jobs = {}
_lock = threading.Lock()
LOOKUP_PROGRESS_INTERVAL = 5


def parse_gpx(content):
    if len(content) > 50 * 1024 * 1024:
        raise ValueError("GPX exceeds 50 MB.")
    try:
        gpx = gpxpy.parse(content.decode("utf-8-sig"))
    except Exception as error:
        raise ValueError(f"Invalid GPX file: {error}") from error
    tracks = [segment.points for track in gpx.tracks for segment in track.segments if segment.points]
    routes = [route.points for route in gpx.routes if route.points]
    paths = tracks or routes
    if len(paths) != 1:
        raise ValueError("Upload one continuous GPX track segment or route; separate tracks cannot be joined safely.")
    records = []
    for point in paths[0]:
        if not (-90 <= point.latitude <= 90 and -180 <= point.longitude <= 180):
            raise ValueError("GPX contains coordinates outside latitude/longitude bounds.")
        if point.elevation is None or not math.isfinite(point.elevation):
            raise ValueError("Every GPX point must have a finite elevation.")
        records.append((point.latitude, point.longitude, point.elevation))
    if len(records) < 2:
        raise ValueError("GPX must contain at least two points.")
    return pd.DataFrame(records, columns=["lat", "lon", "elevation_m"])


def route_frame(db, race_id):
    if db.race(race_id)["route_status"] != "ready":
        raise ValueError("Complete GPX lookup first.")
    route = db.lookup_route(race_id)
    if route["excluded"].isna().any():
        raise ValueError("Resume lookup to complete the route.")
    route = route.loc[route["excluded"].eq(False)].copy()
    if route.empty:
        raise ValueError("No included route points remain.")
    route["route_order"] = route["route_order"].astype("int64")
    route = route.reset_index(drop=True)
    breaks = route["_route_path"].ne(route["_route_path"].shift()) | route["route_order"].diff().ne(1)
    route["_route_path"] = breaks.cumsum() - 1
    return route


def fylke_options(route):
    return sorted(route["fylke"].dropna().unique().tolist())


def filter_contacts(contacts, fylke):
    if fylke is None:
        return contacts.copy().reset_index(drop=True)
    return contacts.loc[contacts["Fylke"].eq(fylke)].reset_index(drop=True)


def filter_route(route, fylke, distance_range=None):
    mask = pd.Series(True, index=route.index)
    if distance_range is not None:
        mask &= route["distance_cum_km"].between(*distance_range)
    if fylke is not None:
        mask &= route["fylke"].eq(fylke)
    selected = route.loc[mask].copy()
    if not selected.empty:
        breaks = selected["_route_path"].ne(selected["_route_path"].shift()) | selected["route_order"].diff().ne(1)
        selected["_route_path"] = breaks.cumsum() - 1
    return selected


def grouped_road_list(route):
    headings = {
        "EV": "EV (Europavei)", "RV": "RV (Riksveg)", "FV": "FV (Fylkesveg)",
        "KV": "KV (Kommunal veg)", "PV/SV": "PV/SV (Privat veg)",
    }
    roads = {category: set() for category in headings}
    other = set()
    for value, name_value in route[["vei_id", "roadname"]].itertuples(index=False, name=None):
        road_id = "" if pd.isna(value) else str(value).strip()
        if not road_id:
            continue
        road_name = "" if pd.isna(name_value) else str(name_value).strip()
        label = f"{road_name or 'Ukjent vegnavn'} ({road_id})"
        category = road_id[:2].upper()
        if category in ("PV", "SV"):
            category = "PV/SV"
        if category in roads:
            roads[category].add(label)
        else:
            other.add(label)
    groups = {heading: sorted(roads[category], key=str.casefold) for category, heading in headings.items()}
    if other:
        groups["Andre veger"] = sorted(other, key=str.casefold)
    return groups


def job_state(db, race_id):
    with _lock:
        job = _jobs.get((db.db_name, race_id))
        return {key: value for key, value in job.items() if key != "cancel"} if job else None


def cancel_job(db, race_id):
    with _lock:
        job = _jobs.get((db.db_name, race_id))
        if job:
            job["cancel"].set()


def start_lookup(db, race_id):
    race = db.race(race_id)
    if not race["gpx_hash"]:
        raise ValueError("Upload GPX first.")
    key = (db.db_name, race_id)
    with _lock:
        if key in _jobs and _jobs[key]["status"] == "running":
            return
        _jobs[key] = {"status": "running", "done": 0, "total": len(db.route_points(race_id)),
                      "message": "Looking up roads and NVDB datasets", "cancel": threading.Event()}
    db.set_status(race_id, "processing")
    _executor.submit(_lookup_worker, db.db_name, race_id, race["gpx_hash"], bool(race["lookup_properties"]))


def _lookup_worker(db_path, race_id, digest, lookup_properties):
    db = data_handler(db_path)
    key = (db_path, race_id)
    rows = db.read_table("road_data", race_id).to_dict("records")
    dirty = []
    try:
        points = db.route_points(race_id)
        completed = next((index for index, row in enumerate(rows)
                          if not row["nvdb_lookup_status"]
                          or (lookup_properties and row["property_lookup_status"] != "complete")), len(rows))
        with _lock:
            _jobs[key].update(done=completed, total=len(points), message="Looking up roads and NVDB datasets")
        municipalities = db.municipalities().set_index("kommunenummer")
        counters = Counter(row["vei_id"] for row in rows if row["vei_id"])
        names = {}
        nvdb_cache = {}
        for row in rows:
            reference = row["kortform"]
            municipality = normalize_municipality_number(row["kommunenr"]) if row["kommunenr"] else None
            if row["nvdb_lookup_status"] and row["nvdb_reference"] == f"{municipality} {reference}":
                nvdb_cache[(municipality, reference)] = {column: row[column] for column in NVDB_COLUMNS}

        def check_cancelled():
            with _lock:
                cancelled = _jobs[key]["cancel"].is_set()
            if cancelled:
                raise InterruptedError("Lookup cancelled. Saved points can be resumed.")

        def road_details(session, reference, municipality):
            check_cancelled()
            municipality = normalize_municipality_number(municipality) if municipality else None
            cache_key = (municipality, reference)
            if cache_key not in nvdb_cache:
                nvdb_cache[cache_key] = lookup_route_data(session, reference, municipality)
            return nvdb_cache[cache_key]

        distances = [0.0] + [lookup.haversine_m(*previous, *current) / 1000
            for previous, current in zip(points[["lat", "lon"]].to_numpy()[:-1], points[["lat", "lon"]].to_numpy()[1:])]
        cumulative = np.cumsum(distances)
        changes = points["elevation_m"].diff().fillna(0).to_numpy()
        ascent, descent = np.maximum(changes, 0).cumsum(), np.maximum(-changes, 0).cumsum()
        with requests.Session() as session:
            adapter = HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504]))
            session.mount("https://", adapter)
            for index, row in enumerate(rows):
                check_cancelled()
                dirty.append(row)
                if not row["nvdb_lookup_status"]:
                    row.update(road_details(session, row["kortform"], row["kommunenr"]))
                if lookup_properties and row["property_lookup_status"] != "complete":
                    point = points.iloc[index]
                    row["matrikkelnummer"] = lookup.lookup_property_details(session, point.lat, point.lon)["matrikkelnummer"]
                    row["property_lookup_status"] = "complete"
                elif not row["property_lookup_status"]:
                    row["property_lookup_status"] = "complete" if row["matrikkelnummer"] else "skipped"
                if (index + 1) % 100 == 0:
                    db.checkpoint_roads(race_id, pd.DataFrame(dirty), digest)
                    dirty.clear()
                if (index + 1) % LOOKUP_PROGRESS_INTERVAL == 0:
                    with _lock:
                        _jobs[key]["done"] = max(_jobs[key]["done"], index + 1)
            with _lock:
                _jobs[key]["done"] = len(rows)
            for index in range(len(rows), len(points)):
                check_cancelled()
                point = points.iloc[index]
                match = lookup.lookup_position(session, point.lat, point.lon)
                property_number = None
                if lookup_properties:
                    property_number = lookup.lookup_property_details(session, point.lat, point.lon)["matrikkelnummer"]
                reference, municipality = match if match else (None, None)
                municipality = normalize_municipality_number(municipality) if municipality else None
                road_id = reference.split(" ", 1)[0] if reference else None
                name_key = (municipality, reference)
                if reference and name_key not in names:
                    names[name_key] = lookup.lookup_roadname(session, reference, municipality)
                if road_id:
                    counters[road_id] += 1
                details = municipalities.loc[int(municipality)] if municipality and int(municipality) in municipalities.index else {}
                row = dict(zip(ROAD_COLUMNS, [
                    index + 1, f"{point.lon}, {point.lat}", reference, municipality, road_id,
                    counters[road_id] if road_id else None, distances[index], cumulative[index], ascent[index], descent[index],
                    property_number, reference[:2] if reference else None,
                    details.get("kommunenavn"), details.get("fylke"), details.get("politidistrikt"), False, names.get(name_key),
                ]))
                row.update(road_details(session, reference, municipality))
                row["property_lookup_status"] = "complete" if lookup_properties else "skipped"
                rows.append(row)
                dirty.append(row)
                if (index + 1) % 100 == 0:
                    db.checkpoint_roads(race_id, pd.DataFrame(dirty), digest)
                    dirty.clear()
                if (index + 1) % LOOKUP_PROGRESS_INTERVAL == 0:
                    with _lock:
                        _jobs[key]["done"] = index + 1
            raw = pd.DataFrame(rows, columns=ROAD_COLUMNS)
            raw["jump_flag"] = lookup.find_road_jump_flags(raw["vei_id"].tolist())
            # No smoothing step: every point is included. Set "excluded" here to plug in your own smoothing.
            raw["excluded"] = False
            check_cancelled()
            db.save_route(race_id, raw, digest)
        with _lock:
            _jobs[key].update(status="complete", done=len(points), message="Lookup complete")
    except Exception as error:
        status = "cancelled" if isinstance(error, InterruptedError) else "failed"
        if dirty:
            try:
                db.checkpoint_roads(race_id, pd.DataFrame(dirty), digest)
            except Exception as checkpoint_error:
                error = RuntimeError(f"{error}; checkpoint failed: {checkpoint_error}")
        db.set_status(race_id, status, str(error))
        completed = next((index for index, row in enumerate(rows)
                          if not row["nvdb_lookup_status"]
                          or (lookup_properties and row["property_lookup_status"] != "complete")), len(rows))
        with _lock:
            _jobs[key].update(status=status, done=completed, message=str(error))


def store_gpx(db, race_id, filename, content):
    state = job_state(db, race_id)
    if state and state["status"] == "running":
        raise ValueError("Cancel the running lookup before replacing its GPX.")
    points = parse_gpx(content)
    digest = hashlib.sha256(content).hexdigest()
    db.save_upload(race_id, filename, content, digest, points)
    start_lookup(db, race_id)


def excel_bytes(frame, sheet_name):
    buffer = BytesIO()
    export = frame.copy()
    long_values = []
    for column_index, column in enumerate(frame.columns):
        for row_index, value in enumerate(frame[column]):
            if isinstance(value, str) and len(value) > 32767:
                excel_row = row_index + 2
                export.iat[row_index, column_index] = f"See Long values: row {excel_row}, column {column}"
                for part, start in enumerate(range(0, len(value), 32000), start=1):
                    long_values.append((excel_row, column, part, value[start:start + 32000]))
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        export.to_excel(writer, sheet_name=sheet_name, index=False)
        if long_values:
            pd.DataFrame(long_values, columns=["Excel row", "Column", "Part", "Value"]).to_excel(
                writer, sheet_name="Long values", index=False)
    return buffer.getvalue()