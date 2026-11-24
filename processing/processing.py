"""GPX parsing, route lookup and export helpers."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import hashlib
import math
import os
import threading

import gpxpy
import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import road_lookup as lookup, route_viewer as viewer
from .data_handler import ROAD_COLUMNS, data_handler
from .nvdb_data import NVDB_COLUMNS, lookup_route_data_many, normalize_municipality_number

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="race-lookup")
_jobs = {}
_lock = threading.Lock()
# Parallel HTTP requests per lookup (set LOOKUP_WORKERS on Railway to tune); NVDB retries on 429.
LOOKUP_WORKERS = int(os.environ.get("LOOKUP_WORKERS", "8"))
LOOKUP_BATCH = 100


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


def _register_job(db, race_id):
    key = (db.db_name, race_id)
    with _lock:
        if key in _jobs and _jobs[key]["status"] == "running":
            return False
        _jobs[key] = {"status": "running", "done": 0, "total": len(db.route_points(race_id)),
                      "message": "Looking up roads and NVDB datasets", "cancel": threading.Event()}
    db.set_status(race_id, "processing")
    return True


def start_lookup(db, race_id):
    race = db.race(race_id)
    if not race["gpx_hash"]:
        raise ValueError("Upload GPX first.")
    if _register_job(db, race_id):
        _executor.submit(_lookup_worker, db.db_name, race_id, race["gpx_hash"], bool(race["lookup_properties"]))


def run_lookup(db, race_id):
    """Run the road lookup in the calling thread; raise if it does not complete."""
    race = db.race(race_id)
    if not race["gpx_hash"]:
        raise ValueError("Upload GPX first.")
    if not _register_job(db, race_id):
        raise RuntimeError("A lookup is already running for this race.")
    try:
        _lookup_worker(db.db_name, race_id, race["gpx_hash"], bool(race["lookup_properties"]))
    finally:
        with _lock:
            job = _jobs.pop((db.db_name, race_id))
    if job["status"] != "complete":
        raise RuntimeError(f"Road lookup {job['status']}: {job['message']}")


def _lookup_worker(db_path, race_id, digest, lookup_properties):
    db = data_handler(db_path)
    key = (db_path, race_id)
    rows = db.read_table("road_data", race_id).to_dict("records")
    dirty = []
    pool = None
    try:
        points = db.route_points(race_id)
        completed = next((index for index, row in enumerate(rows)
                          if _needs_nvdb(row)
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
            if row["nvdb_lookup_status"] and not _needs_nvdb(row) and row["nvdb_reference"] == f"{municipality} {reference}":
                nvdb_cache[(municipality, reference)] = {column: row[column] for column in NVDB_COLUMNS}

        def check_cancelled():
            with _lock:
                cancelled = _jobs[key]["cancel"].is_set()
            if cancelled:
                raise InterruptedError("Lookup cancelled. Saved points can be resumed.")

        def cache_key(reference, municipality):
            return normalize_municipality_number(municipality) if municipality else None, reference

        def fetch_road_details(session, pool, pairs):
            missing = {}
            for reference, municipality in pairs:
                municipality, reference = cache_key(reference, municipality)
                if (municipality, reference) not in nvdb_cache:
                    missing[(municipality, reference)] = (reference, municipality)
            found = lookup_route_data_many(session, missing.values(), pool)
            for cached, lookup_key in missing.items():
                nvdb_cache[cached] = found[lookup_key]

        def road_details(reference, municipality):
            return nvdb_cache[cache_key(reference, municipality)]

        coords = points[["lat", "lon"]].to_numpy()

        def property_number(session, index):
            return lookup.lookup_property_details(session, *coords[index])["matrikkelnummer"]

        def set_done(done):
            with _lock:
                _jobs[key]["done"] = max(_jobs[key]["done"], done)

        distances = [0.0] + [lookup.haversine_m(*previous, *current) / 1000
            for previous, current in zip(coords[:-1], coords[1:])]
        cumulative = np.cumsum(distances)
        changes = points["elevation_m"].diff().fillna(0).to_numpy()
        ascent, descent = np.maximum(changes, 0).cumsum(), np.maximum(-changes, 0).cumsum()
        pool = ThreadPoolExecutor(max_workers=LOOKUP_WORKERS, thread_name_prefix="nvdb-request")
        with requests.Session() as session:
            adapter = HTTPAdapter(pool_maxsize=LOOKUP_WORKERS, max_retries=Retry(
                total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504]))
            session.mount("https://", adapter)
            # Requests within a batch run in parallel; rows are assembled in order so counters stay sequential.
            for start in range(0, len(rows), LOOKUP_BATCH):
                check_cancelled()
                batch = list(enumerate(rows[start:start + LOOKUP_BATCH], start))
                property_futures = {
                    index: pool.submit(property_number, session, index) for index, row in batch
                    if lookup_properties and row["property_lookup_status"] != "complete"}
                fetch_road_details(session, pool, [(row["kortform"], row["kommunenr"])
                                                   for _, row in batch if _needs_nvdb(row)])
                for index, row in batch:
                    dirty.append(row)
                    if _needs_nvdb(row):
                        row.update(road_details(row["kortform"], row["kommunenr"]))
                    if index in property_futures:
                        row["matrikkelnummer"] = property_futures[index].result()
                        row["property_lookup_status"] = "complete"
                    elif not row["property_lookup_status"]:
                        row["property_lookup_status"] = "complete" if row["matrikkelnummer"] else "skipped"
                db.checkpoint_roads(race_id, pd.DataFrame(dirty), digest)
                dirty.clear()
                set_done(start + len(batch))
            set_done(len(rows))
            for start in range(len(rows), len(points), LOOKUP_BATCH):
                check_cancelled()
                indices = range(start, min(start + LOOKUP_BATCH, len(points)))
                position_futures = [pool.submit(lookup.lookup_position, session, *coords[index]) for index in indices]
                property_futures = [pool.submit(property_number, session, index) if lookup_properties else None
                                    for index in indices]
                matches = []
                for future in position_futures:
                    match = future.result()
                    reference, municipality = match if match else (None, None)
                    matches.append((reference, normalize_municipality_number(municipality) if municipality else None))
                name_futures = {}
                for reference, municipality in matches:
                    if reference and (municipality, reference) not in names and (municipality, reference) not in name_futures:
                        name_futures[(municipality, reference)] = pool.submit(
                            lookup.lookup_roadname, session, reference, municipality)
                fetch_road_details(session, pool, matches)
                for name_key, future in name_futures.items():
                    names[name_key] = future.result()
                for index, (reference, municipality), property_future in zip(indices, matches, property_futures):
                    point = points.iloc[index]
                    property_number_value = property_future.result() if property_future else None
                    road_id = reference.split(" ", 1)[0] if reference else None
                    name_key = (municipality, reference)
                    if road_id:
                        counters[road_id] += 1
                    details = municipalities.loc[int(municipality)] if municipality and int(municipality) in municipalities.index else {}
                    row = dict(zip(ROAD_COLUMNS, [
                        index + 1, f"{point.lon}, {point.lat}", reference, municipality, road_id,
                        counters[road_id] if road_id else None, distances[index], cumulative[index], ascent[index], descent[index],
                        property_number_value, reference[:2] if reference else None,
                        details.get("kommunenavn"), details.get("fylke"), details.get("politidistrikt"), False, names.get(name_key),
                    ]))
                    row.update(road_details(reference, municipality))
                    row["property_lookup_status"] = "complete" if lookup_properties else "skipped"
                    rows.append(row)
                    dirty.append(row)
                db.checkpoint_roads(race_id, pd.DataFrame(dirty), digest)
                dirty.clear()
                set_done(indices.stop)
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
                          if _needs_nvdb(row)
                          or (lookup_properties and row["property_lookup_status"] != "complete")), len(rows))
        with _lock:
            _jobs[key].update(status=status, done=completed, message=str(error))
    finally:
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


def _needs_nvdb(row):
    # Points looked up before road surface was added are looked up again to fill it in.
    return not row["nvdb_lookup_status"] or (row["nvdb_lookup_status"] == "complete" and not row["surface_type"])


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
