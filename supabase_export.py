"""Three-table Supabase exports; no network connection or credentials required."""

from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import numpy as np
import pandas as pd

from processing.data_handler import NUMERIC, POINT_COLUMNS, RACE_METADATA_FIELDS, SEGMENT_COLUMNS, quoted
from processing.nvdb_data import DATASET_COLUMNS, normalize_municipality_number

SCHEMA_FILE = Path(__file__).with_name("supabase_schema.sql")
RACE_COLUMNS = [
    "race_id", "race_name", "race_date", *RACE_METADATA_FIELDS,
    "gpx_filename", "gpx_hash", "lookup_properties", "route_status", "updated_at", "elevation_extrema",
]
ROUTE_COLUMNS = ["race_id", *POINT_COLUMNS]
INTEGER_COLUMNS = {
    "route_order", "smoothed_source_route_order", "counter", "traffic_year", "lane_count", "cycle_lane_count",
    "functional_road_class", "path_id", *(column for column in DATASET_COLUMNS if column.endswith("_count")),
}
BOOLEAN_COLUMNS = {"excluded", "jump_flag"}
CONTACT_EXPORT_COLUMNS = {
    "contact_id": "contact_id", "Type": "contact_type", "Navn": "name", "Fylke": "fylke",
    "Epost": "email", "Telefonnummer": "phone", "Lenke": "link",
}


def export_frames(db, race_id=None):
    with db.connection() as connection:
        connection.execute("BEGIN")
        query = f"""SELECT {','.join(map(quoted, RACE_COLUMNS))} FROM racemaster AS r
            WHERE route_status='ready' AND coefficients_json IS NOT NULL
            AND EXISTS (SELECT 1 FROM route_segments s WHERE s.race_id=r.race_id AND total_risk IS NOT NULL)"""
        if race_id is not None:
            query += " AND r.race_id=?"
        races = pd.read_sql_query(query + " ORDER BY race_id", connection,
                                  params=() if race_id is None else (race_id,))
        if races.empty:
            raise ValueError("Fit elevation, generate segments and save segment risks before exporting to Supabase.")
        routes = []
        segments = []
        for identifier in races["race_id"]:
            route = pd.read_sql_query(
                f"SELECT {','.join(map(quoted, ROUTE_COLUMNS))} FROM route_data WHERE race_id=? ORDER BY route_order",
                connection, params=(identifier,))
            section = pd.read_sql_query(
                "SELECT * FROM route_segments WHERE race_id=? ORDER BY segment",
                connection, params=(identifier,))
            if route.empty or route[["excluded", "fitted_elevation_m"]].isna().any().any():
                raise ValueError(f"Race {identifier}: GPX lookup or elevation fit is incomplete.")
            routes.append(route)
            segments.append(section)
    route = pd.concat(routes, ignore_index=True).reindex(columns=ROUTE_COLUMNS)
    for column in (NUMERIC - BOOLEAN_COLUMNS).intersection(route.columns) | {
            "lat", "lon", "elevation_m", "path_id", "fitted_elevation_m"}:
        values = pd.to_numeric(route[column], errors="raise")
        if not np.isfinite(values.dropna().to_numpy()).all():
            raise ValueError(f"Supabase export: {column} contains non-finite values.")
        if column in INTEGER_COLUMNS:
            if values.dropna().mod(1).ne(0).any():
                raise ValueError(f"Supabase export: {column} must contain integers.")
            values = values.astype("Int64")
        route[column] = values
    if not route["lat"].between(-90, 90).all() or not route["lon"].between(-180, 180).all():
        raise ValueError("Supabase export: route coordinates are outside valid bounds.")
    if route["elevation_m"].isna().any() or route["route_order"].le(0).any():
        raise ValueError("Supabase export: elevation and positive route order are required.")
    for column in BOOLEAN_COLUMNS:
        route[column] = route[column].astype("boolean").map({True: "true", False: "false"})
    route["kommunenr"] = route["kommunenr"].map(
        lambda value: normalize_municipality_number(value) if pd.notna(value) else None)
    races["race_date"] = races["race_date"].replace("", None)
    races["lookup_properties"] = races["lookup_properties"].astype("boolean").map({True: "true", False: "false"})
    races["updated_at"] = pd.to_datetime(races["updated_at"], utc=True, errors="raise").map(lambda value: value.isoformat())
    section = pd.concat(segments, ignore_index=True).reindex(columns=["race_id", *SEGMENT_COLUMNS])
    section["risk_peak_route_order"] = section["risk_peak_route_order"].astype("Int64")
    return races, route, section


def export_contacts(db):
    with db.connection() as connection:
        fields = ",".join(map(quoted, CONTACT_EXPORT_COLUMNS))
        contacts = pd.read_sql_query(f'SELECT {fields} FROM contacts ORDER BY "Fylke","Type","Navn"', connection)
    return contacts.rename(columns=CONTACT_EXPORT_COLUMNS).fillna("")


def supabase_package(db, race_id=None):
    races, route, segments = export_frames(db, race_id)
    contacts = export_contacts(db)
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("supabase_schema.sql", SCHEMA_FILE.read_text(encoding="utf-8"))
        archive.writestr("racemaster.csv", races.to_csv(index=False, lineterminator="\n"))
        archive.writestr("route_data.csv", route.to_csv(index=False, lineterminator="\n"))
        archive.writestr("route_segments.csv", segments.to_csv(index=False, lineterminator="\n"))
        archive.writestr("contacts.csv", contacts.to_csv(index=False, lineterminator="\n"))
    return buffer.getvalue()
