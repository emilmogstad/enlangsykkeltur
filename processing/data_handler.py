"""Transactional, race-scoped SQLite storage."""

from contextlib import closing, contextmanager
from datetime import date
import json
from pathlib import Path
import sqlite3
import uuid
import zlib

import pandas as pd
from .nvdb_data import NVDB_COLUMNS, NVDB_NUMERIC

KOMMUNE_TABLE = """CREATE TABLE IF NOT EXISTS kommune_info (
    kommunenummer INTEGER PRIMARY KEY, kommunenavn TEXT NOT NULL,
    fylke TEXT, politidistrikt TEXT)"""

RISK_COLUMNS = ["traffic_risk", "environmental_risk", "crash_risk", "fatigue_risk", "remoteness_risk", "aggregate_risk"]
SMOOTHING_COLUMNS = ["smoothed_vei_id", "smoothed_source_route_order"]
ROAD_ATTRIBUTE_COLUMNS = [
    "kortform", "kommunenr", "vei_id", "matrikkelnummer", "veitype",
    "kommunename", "fylke", "politidistrikt", "roadname",
    *NVDB_COLUMNS, "property_lookup_status",
]
ROAD_COLUMNS = [
    "route_order", "lon_lat", "kortform", "kommunenr", "vei_id", "counter",
    "distance_km", "distance_cum_km", "elev_gain_cum_m", "elev_loss_cum_m",
    "matrikkelnummer", "veitype", "kommunename", "fylke", "politidistrikt", "jump_flag", "roadname",
    *NVDB_COLUMNS,
    "property_lookup_status", "excluded",
    *SMOOTHING_COLUMNS,
    *RISK_COLUMNS,
]
LOOKUP_COLUMNS = [c for c in ROAD_COLUMNS if c not in {"route_order", "lon_lat", "counter", *RISK_COLUMNS}]
POINT_COLUMNS = ["route_order", "lat", "lon", "elevation_m", "path_id", *LOOKUP_COLUMNS,
                 "fitted_elevation_m", "elevation_extremum"]
SEGMENT_RISK_COLUMNS = [
    "total_risk", "risk_peak_route_order",
    *(f"max_{c}" for c in RISK_COLUMNS[:-1]), *(f"peak_{c}" for c in RISK_COLUMNS[:-1]),
]
SEGMENT_COLUMNS = [
    "segment", "segment_name", "from", "to", "start_route_order", "end_route_order",
    "start_distance_km", "end_distance_km", "distance_km", "elevation_gain_m",
    "elevation_loss_m", "max_abs_gradient_pct", "direction", "average_gradient_pct",
    "strava_score", "strava_equivalent_category", "private_distance_km",
    "fylke", "kommunename", "politidistrikt", "roadname", *SEGMENT_RISK_COLUMNS,
]
NUMERIC = {"route_order", "smoothed_source_route_order", "counter", "distance_km", "distance_cum_km", "elev_gain_cum_m", "elev_loss_cum_m",
           "jump_flag", "excluded", *RISK_COLUMNS, *NVDB_NUMERIC}
RACE_METADATA_FIELDS = (
    "start_location", "finish_location", "organizer", "organizer_address", "organizer_email", "organizer_phone",
)
APP_DIR = Path(__file__).resolve().parent.parent
MUNICIPALITIES_FILE = APP_DIR / "inputs" / "kommune_info.csv"
LEGACY_DB = APP_DIR / "data" / "races.sqlite3"
LOCAL_DB = APP_DIR / "data" / "races_v2.sqlite3"


def quoted(value):
    return '"' + value.replace('"', '""') + '"'


def apply_smoothed_road_data(frame: pd.DataFrame) -> pd.DataFrame:
    """Resolve saved donor rows from original lookup data; preserve all point-specific fields.

    Road counters are recalculated in route order. Call this once on a raw route.
    """
    result = frame.copy()
    assigned = frame["smoothed_source_route_order"].notna()
    if not assigned.any():
        return result
    if frame["route_order"].duplicated().any():
        raise ValueError("Route point orders must be unique to resolve smoothing donors.")
    source = frame.set_index("route_order")
    orders = frame.loc[assigned, "smoothed_source_route_order"]
    if not orders.isin(source.index).all():
        raise ValueError("Smoothed source points are missing. Rerun smoothing.")
    donors = source.loc[orders.tolist()]
    expected = frame.loc[assigned, "smoothed_vei_id"].to_numpy()
    if not pd.Series(donors["vei_id"].to_numpy()).eq(pd.Series(expected)).all():
        raise ValueError("Smoothed source roads do not match the selection. Rerun smoothing.")
    result.loc[assigned, ROAD_ATTRIBUTE_COLUMNS] = donors[ROAD_ATTRIBUTE_COLUMNS].to_numpy()
    ordered = result.sort_values("route_order")
    result["counter"] = (ordered.groupby("vei_id", sort=False).cumcount() + 1).reindex(result.index)
    return result


class data_handler:
    def __init__(self, db_name=None, *, legacy_path=None):
        path = Path(db_name or LOCAL_DB).resolve()
        if path == LEGACY_DB.resolve():
            raise ValueError("The legacy database is read-only; use the new SQLite database.")
        self.db_name = str(path)
        legacy = Path(legacy_path or LEGACY_DB).resolve() if db_name is None or legacy_path else None
        Path(self.db_name).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 2:
                connection.execute("DROP TABLE IF EXISTS contacts")
                connection.execute("DROP TABLE IF EXISTS contact_imports")
                return
            if version or connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
                raise ValueError("Unrecognized database format. Choose an empty destination for migration.")
            connection.execute("""CREATE TABLE racemaster (
                race_id TEXT PRIMARY KEY, race_name TEXT NOT NULL, race_date TEXT NOT NULL,
                start_location TEXT NOT NULL DEFAULT '', finish_location TEXT NOT NULL DEFAULT '',
                organizer TEXT NOT NULL DEFAULT '', organizer_address TEXT NOT NULL DEFAULT '',
                organizer_email TEXT NOT NULL DEFAULT '', organizer_phone TEXT NOT NULL DEFAULT '',
                gpx_filename TEXT, gpx_archive BLOB, gpx_hash TEXT,
                coefficients_json TEXT, elevation_extrema TEXT,
                revision INTEGER NOT NULL DEFAULT 0, fit_revision INTEGER NOT NULL DEFAULT 0,
                lookup_properties INTEGER NOT NULL DEFAULT 0,
                route_status TEXT NOT NULL DEFAULT 'empty', route_error TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            connection.execute(KOMMUNE_TABLE)
            integers = {"smoothed_source_route_order", "jump_flag", "excluded"}
            definitions = ",".join(f'{quoted(c)} '
                f'{"INTEGER" if c in integers else "REAL" if c in NUMERIC else "TEXT"}' for c in LOOKUP_COLUMNS)
            connection.execute(f"""CREATE TABLE route_data (
                race_id TEXT NOT NULL REFERENCES racemaster(race_id) ON DELETE CASCADE,
                route_order INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
                elevation_m REAL NOT NULL, path_id INTEGER NOT NULL DEFAULT 0,
                {definitions}, fitted_elevation_m REAL,
                elevation_extremum TEXT CHECK (elevation_extremum IN ('peak','valley','both')),
                PRIMARY KEY(race_id,route_order)) WITHOUT ROWID""")
            text_fields = {"segment_name", "from", "to", "direction", "strava_equivalent_category",
                           "fylke", "kommunename", "politidistrikt", "roadname"}
            definitions = ",".join(f'{quoted(c)} '
                f'{"TEXT" if c in text_fields else "INTEGER" if c in {"segment", "risk_peak_route_order"} else "REAL"}'
                for c in SEGMENT_COLUMNS)
            connection.execute(f"""CREATE TABLE route_segments (
                race_id TEXT NOT NULL REFERENCES racemaster(race_id) ON DELETE CASCADE,
                {definitions}, PRIMARY KEY(race_id,segment)) WITHOUT ROWID""")
            if legacy is not None and legacy.exists():
                self._migrate(connection, legacy)
            if MUNICIPALITIES_FILE.exists() and not connection.execute("SELECT 1 FROM kommune_info LIMIT 1").fetchone():
                municipalities = pd.read_csv(MUNICIPALITIES_FILE)
                connection.executemany(
                    "INSERT INTO kommune_info (kommunenummer,kommunenavn,fylke,politidistrikt) VALUES (?,?,?,?)",
                    municipalities.astype(object).where(municipalities.notna(), None).itertuples(index=False, name=None))
            connection.execute("PRAGMA user_version=2")

    def _migrate(self, target, legacy):
        from .elev_profile import cosine_family, find_peaks_and_valleys

        with closing(sqlite3.connect(legacy.as_uri() + "?mode=ro", uri=True)) as source:
            source.execute("BEGIN")
            source.row_factory = sqlite3.Row
            tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for row in source.execute("SELECT * FROM racemaster"):
                values = dict(row)
                content = values.pop("gpx_data")
                values["gpx_archive"] = zlib.compress(content) if content else None
                if values["route_status"] == "processing":
                    values["route_status"] = "uploaded"
                fields = ",".join(map(quoted, values))
                target.execute(f"INSERT INTO racemaster ({fields}) VALUES ({','.join('?' for _ in values)})",
                               tuple(values.values()))
            road_columns = {row[1] for row in source.execute("PRAGMA table_info(road_data)")}
            unmatched = source.execute("""SELECT count(*) FROM road_data r WHERE NOT EXISTS (
                SELECT 1 FROM gpx_points p WHERE p.race_id=r.race_id AND p.route_order=r.route_order)""").fetchone()[0]
            if unmatched:
                raise ValueError(f"Migration stopped: {unmatched} lookup rows do not match GPX points.")
            fields = ["race_id", *POINT_COLUMNS[:-2]]
            geometry = ["race_id", "route_order", "lat", "lon", "elevation_m", "path_id"]
            select = [f"p.{c}" for c in geometry] + [
                f"r.{quoted(c)}" if c in road_columns else "NULL" for c in LOOKUP_COLUMNS]
            target.executemany(f"INSERT INTO route_data ({','.join(map(quoted, fields))}) "
                               f"VALUES ({','.join('?' for _ in fields)})",
                               source.execute(f"""SELECT {','.join(select)} FROM gpx_points p LEFT JOIN road_data r
                                   ON p.race_id=r.race_id AND p.route_order=r.route_order"""))
            if "kommune_info" in tables:
                rows = source.execute("SELECT kommunenummer,kommunenavn,fylke,politidistrikt FROM kommune_info")
                target.execute("DELETE FROM kommune_info")
                target.executemany("INSERT INTO kommune_info VALUES (?,?,?,?)", rows)
            if "elevation_fits" in tables:
                for race_id, gpx_hash, encoded in source.execute("""SELECT f.race_id,f.gpx_hash,f.coefficients_json
                    FROM elevation_fits f JOIN racemaster r ON r.race_id=f.race_id AND r.gpx_hash=f.gpx_hash"""):
                    orders = pd.read_sql_query("SELECT route_order FROM route_data WHERE race_id=? ORDER BY route_order",
                                               target, params=(race_id,))["route_order"].to_numpy(dtype=float)
                    coefficients = json.loads(encoded)
                    fitted = cosine_family(orders, coefficients, orders[0], orders[-1])
                    extrema = find_peaks_and_valleys(coefficients, orders[0], orders[-1])
                    self._store_fit(target, race_id, gpx_hash, coefficients, orders, fitted, extrema)

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.db_name, timeout=30)
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def races(self):
        with self.connection() as connection:
            return pd.read_sql_query("SELECT race_id,race_name,race_date,route_status,route_error,gpx_filename "
                                     "FROM racemaster ORDER BY race_date DESC,race_name", connection)

    def create_race(self, race_name, race_date=None, *, start_location="", finish_location="", organizer="",
                    organizer_address="", organizer_email="", organizer_phone=""):
        if not str(race_name).strip():
            raise ValueError("Race name is required.")
        race_id = str(uuid.uuid4())
        with self.connection() as connection:
            connection.execute("""INSERT INTO racemaster
                (race_id,race_name,race_date,start_location,finish_location,organizer,
                 organizer_address,organizer_email,organizer_phone) VALUES (?,?,?,?,?,?,?,?,?)""",
                (race_id, str(race_name).strip(), date.fromisoformat(str(race_date)).isoformat() if race_date else "",
                 start_location.strip(), finish_location.strip(), organizer.strip(),
                 organizer_address.strip(), organizer_email.strip(), organizer_phone.strip()))
        return race_id

    def race(self, race_id):
        with self.connection() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("""SELECT race_id,race_name,race_date,start_location,finish_location,
                organizer,organizer_address,organizer_email,organizer_phone,gpx_filename,gpx_hash,
                lookup_properties,route_status,route_error,updated_at,revision,fit_revision
                FROM racemaster WHERE race_id=?""", (race_id,)).fetchone()
        if row is None:
            raise ValueError("Race does not exist.")
        return dict(row)

    def gpx_content(self, race_id):
        with self.connection() as connection:
            row = connection.execute("SELECT gpx_archive FROM racemaster WHERE race_id=?", (race_id,)).fetchone()
        return zlib.decompress(row[0]) if row and row[0] else None

    def update_race(self, race_id, name, race_date, *, start_location, finish_location, organizer,
                    organizer_address, organizer_email, organizer_phone):
        if not str(name).strip():
            raise ValueError("Race name is required.")
        with self.connection() as connection:
            connection.execute("""UPDATE racemaster SET race_name=?,race_date=?,start_location=?,finish_location=?,
                organizer=?,organizer_address=?,organizer_email=?,organizer_phone=?,
                updated_at=CURRENT_TIMESTAMP WHERE race_id=?""",
                (str(name).strip(), date.fromisoformat(str(race_date)).isoformat() if race_date else "",
                 start_location.strip(), finish_location.strip(), organizer.strip(),
                 organizer_address.strip(), organizer_email.strip(), organizer_phone.strip(), race_id))

    def set_status(self, race_id, status, error=None):
        with self.connection() as connection:
            connection.execute("UPDATE racemaster SET route_status=?,route_error=?,updated_at=CURRENT_TIMESTAMP WHERE race_id=?",
                               (status, error, race_id))
            if status == "processing":
                self._invalidate(connection, race_id)

    def set_lookup_properties(self, race_id, enabled):
        with self.connection() as connection:
            connection.execute("UPDATE racemaster SET lookup_properties=?,updated_at=CURRENT_TIMESTAMP WHERE race_id=?",
                               (int(enabled), race_id))

    def read_table(self, table, race_id, *, smoothed=False):
        if table != "road_data":
            raise ValueError("Unknown race table.")
        with self.connection() as connection:
            frame = self._read_lookup(connection, race_id)
        return apply_smoothed_road_data(frame) if smoothed else frame

    def lookup_route(self, race_id, *, smoothed=True):
        with self.connection() as connection:
            connection.execute("BEGIN")
            frame = self._read_lookup(connection, race_id, geometry=True)
            count = connection.execute("SELECT count(*) FROM route_data WHERE race_id=?", (race_id,)).fetchone()[0]
        if frame.empty or len(frame) != count:
            raise ValueError("The stored route is incomplete.")
        return apply_smoothed_road_data(frame) if smoothed else frame

    @staticmethod
    def _read_lookup(connection, race_id, *, geometry=False):
        frame = pd.read_sql_query("""SELECT * FROM route_data WHERE race_id=?
            AND (nvdb_lookup_status IS NOT NULL OR excluded IS NOT NULL) ORDER BY route_order""",
            connection, params=(race_id,))
        frame["lon_lat"] = [f"{lon}, {lat}" for lon, lat in zip(frame["lon"], frame["lat"])]
        frame["counter"] = frame.groupby("vei_id", sort=False).cumcount() + 1
        frame["excluded"] = frame["excluded"].astype("boolean")
        columns = ROAD_COLUMNS + (["lat", "lon", "elevation_m", "_route_path"] if geometry else [])
        return frame.rename(columns={"path_id": "_route_path"}).reindex(columns=columns)

    @staticmethod
    def _invalidate(connection, race_id):
        connection.execute("DELETE FROM route_segments WHERE race_id=?", (race_id,))
        connection.execute("""UPDATE racemaster SET revision=revision+1,updated_at=CURRENT_TIMESTAMP
            WHERE race_id=?""", (race_id,))

    def _write_lookup(self, connection, race_id, frame):
        fields = ["route_order", *LOOKUP_COLUMNS]
        rows = frame.reindex(columns=fields)
        if rows["route_order"].duplicated().any():
            raise ValueError("Lookup contains duplicate route points.")
        rows = rows.astype(object).where(rows.notna(), None)
        assignments = ",".join(f"{quoted(c)}=?" for c in LOOKUP_COLUMNS)
        updated = connection.executemany(f"UPDATE route_data SET {assignments} WHERE race_id=? AND route_order=?",
            [(*values, race_id, order) for order, *values in rows.itertuples(index=False, name=None)])
        if updated.rowcount != len(rows):
            raise ValueError("Lookup rows do not match the uploaded GPX.")

    def save_upload(self, race_id, filename, content, digest, points):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("SELECT gpx_hash FROM racemaster WHERE race_id=?", (race_id,)).fetchone()
            if previous is None:
                raise ValueError("Race does not exist.")
            if previous[0] == digest:
                connection.execute("UPDATE racemaster SET gpx_filename=? WHERE race_id=?",
                                   (Path(filename).name, race_id))
                return
            self._invalidate(connection, race_id)
            connection.execute("DELETE FROM route_data WHERE race_id=?", (race_id,))
            point_rows = [
                (race_id, index, float(row.lat), float(row.lon), float(row.elevation_m), getattr(row, "path_id", 0))
                for index, row in enumerate(points.itertuples(index=False), 1)
            ]
            point_insert = "INSERT INTO route_data (race_id,route_order,lat,lon,elevation_m,path_id) VALUES (?,?,?,?,?,?)"
            connection.executemany(point_insert, point_rows)
            connection.execute("""UPDATE racemaster SET gpx_filename=?,gpx_archive=?,gpx_hash=?,
                coefficients_json=NULL,elevation_extrema=NULL,fit_revision=fit_revision+1,
                route_status='uploaded',route_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE race_id=?""",
                (Path(filename).name, zlib.compress(content), digest, race_id))

    def route_points(self, race_id):
        with self.connection() as connection:
            return pd.read_sql_query("SELECT route_order,lat,lon,elevation_m,path_id AS _route_path FROM route_data WHERE race_id=? ORDER BY route_order",
                                     connection, params=(race_id,))

    def save_elevation_fit(self, race_id, gpx_hash, coefficients, fitted, extrema):
        with self.connection() as connection:
            self._store_fit(connection, race_id, gpx_hash, coefficients, fitted["route_order"],
                            fitted["fitted_elevation_m"], extrema)

    def _store_fit(self, connection, race_id, gpx_hash, coefficients, orders, heights, extrema):
        cursor = connection.execute("""UPDATE racemaster SET coefficients_json=?,elevation_extrema=?,
            fit_revision=fit_revision+1 WHERE race_id=? AND gpx_hash=?""",
            (json.dumps(coefficients, allow_nan=False), extrema.to_json(orient="records", double_precision=15),
             race_id, gpx_hash))
        if cursor.rowcount != 1:
            raise ValueError("The GPX changed during fitting. Reload the page and fit again.")
        markers = {}
        for order, kind in zip(extrema["route_order"], extrema["kind"]):
            point = int(round(order))
            previous = markers.get(point)
            markers[point] = "both" if previous and previous != kind else kind
        connection.executemany("""UPDATE route_data SET fitted_elevation_m=?,elevation_extremum=?
            WHERE race_id=? AND route_order=?""",
            ((float(height), markers.get(int(order)), race_id, int(order)) for order, height in zip(orders, heights)))
        self._invalidate(connection, race_id)

    def load_elevation_fit(self, race_id, gpx_hash):
        with self.connection() as connection:
            connection.execute("BEGIN")
            row = connection.execute("""SELECT coefficients_json,elevation_extrema FROM racemaster
                WHERE race_id=? AND gpx_hash=? AND coefficients_json IS NOT NULL""", (race_id, gpx_hash)).fetchone()
            if row is None:
                return None
            points = pd.read_sql_query("SELECT route_order,fitted_elevation_m FROM route_data WHERE race_id=? ORDER BY route_order",
                                       connection, params=(race_id,))
        return json.loads(row[0]), pd.DataFrame(json.loads(row[1]), columns=["route_order", "fitted_elevation_m", "kind"]), points

    def segments(self, race_id):
        with self.connection() as connection:
            return pd.read_sql_query("SELECT * FROM route_segments WHERE race_id=? ORDER BY segment", connection,
                                     params=(race_id,), dtype={"total_risk": "float64"}).drop(columns="race_id")

    def save_segments(self, race_id, revision, segments):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._check_revision(connection, race_id, revision)
            if not connection.execute("SELECT coefficients_json FROM racemaster WHERE race_id=?", (race_id,)).fetchone()[0]:
                raise ValueError("Fit the elevation profile before saving segments.")
            self._invalidate(connection, race_id)
            fields = ["race_id", *SEGMENT_COLUMNS]
            rows = segments.reindex(columns=SEGMENT_COLUMNS).astype(object)
            rows = rows.where(rows.notna(), None)
            connection.executemany(f"INSERT INTO route_segments ({','.join(map(quoted, fields))}) "
                                   f"VALUES ({','.join('?' for _ in fields)})",
                                   ((race_id, *row) for row in rows.itertuples(index=False, name=None)))

    @staticmethod
    def _check_revision(connection, race_id, revision):
        row = connection.execute("SELECT revision,route_status FROM racemaster WHERE race_id=?", (race_id,)).fetchone()
        if row != (revision, "ready"):
            raise ValueError("The route changed. Reload the page and try again.")

    def save_segment_risks(self, race_id, revision, risks):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._check_revision(connection, race_id, revision)
            rows = risks[["segment", *SEGMENT_RISK_COLUMNS]].astype(object)
            count = connection.execute("SELECT count(*) FROM route_segments WHERE race_id=?", (race_id,)).fetchone()[0]
            if not count or len(rows) != count or rows["segment"].duplicated().any():
                raise ValueError("Risk calculation must cover every saved segment.")
            rows = rows.where(rows.notna(), None)
            assignments = ",".join(f"{quoted(c)}=?" for c in SEGMENT_RISK_COLUMNS)
            updated = connection.executemany(f"UPDATE route_segments SET {assignments} WHERE race_id=? AND segment=?",
                ((*values, race_id, segment) for segment, *values in rows.itertuples(index=False, name=None)))
            if updated.rowcount != len(rows):
                raise ValueError("The segments changed. Reload the page and try again.")
            connection.execute("UPDATE racemaster SET updated_at=CURRENT_TIMESTAMP WHERE race_id=?", (race_id,))

    def save_route(self, race_id, route, expected_hash):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT gpx_hash FROM racemaster WHERE race_id=?", (race_id,)).fetchone()
            if current is None or current[0] != expected_hash:
                raise ValueError("The GPX changed during processing. Run lookup again.")
            if len(route) != connection.execute("SELECT count(*) FROM route_data WHERE race_id=?", (race_id,)).fetchone()[0]:
                raise ValueError("Lookup must cover every GPX point before completion.")
            self._invalidate(connection, race_id)
            connection.execute("UPDATE route_data SET smoothed_vei_id=NULL,smoothed_source_route_order=NULL WHERE race_id=?", (race_id,))
            self._write_lookup(connection, race_id, route.drop(columns=SMOOTHING_COLUMNS, errors="ignore"))
            connection.execute("UPDATE racemaster SET route_status='ready',route_error=NULL WHERE race_id=?", (race_id,))

    def save_smoothed_vei_id(self, race_id, frame, *, revision):
        """Store selected road and donor point without modifying original lookup attributes."""
        rows = [(None if pd.isna(vei_id) else str(vei_id),
                 None if pd.isna(source_order) else int(source_order), race_id, int(route_order))
                for route_order, vei_id, source_order in
                frame[["route_order", *SMOOTHING_COLUMNS]].itertuples(index=False, name=None)]
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._check_revision(connection, race_id, revision)
            self._invalidate(connection, race_id)
            updated = connection.executemany("""UPDATE route_data SET smoothed_vei_id=?, smoothed_source_route_order=?
                WHERE race_id=? AND route_order=?""", rows)
            if updated.rowcount != len(rows):
                raise ValueError("Smoothed rows do not match the uploaded GPX.")

    def checkpoint_roads(self, race_id, frame, expected_hash):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT gpx_hash FROM racemaster WHERE race_id=?", (race_id,)).fetchone()
            if current is None or current[0] != expected_hash:
                raise ValueError("The GPX changed during lookup.")
            self._write_lookup(connection, race_id, frame)

    def municipalities(self):
        with self.connection() as connection:
            return pd.read_sql_query("SELECT * FROM kommune_info ORDER BY kommunenummer", connection)
