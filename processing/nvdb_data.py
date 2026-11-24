"""Additional NVDB datasets at a route's road reference."""

import math
import re

import requests

OBJECT_URL = "https://nvdbapiles.atlas.vegvesen.no/vegobjekter/api/v4/vegobjekter"
HEADERS = {"Accept": "application/json", "X-Client": "BikingMan-Norway-route-lookups"}
NEARBY_METRES = 25
# Point-like facilities and accidents use a small along-road search interval.
DATASETS = (
    ("traffic", 540, False),
    ("speed_limit", 105, False),
    ("tunnel", 67, False),
    ("bridge", 60, False),
    ("calculated_road_width", 838, False),
    ("lanes", 616, False),
    ("road_class", 821, False),
    ("accident", 570, True),
    ("cattle_grid", 22, True),
    ("speed_bump", 103, True),
    ("surface", 241, False),
)
SURFACE_LAYER = 8741
SURFACE_MATERIAL = 1216
# NVDB Massetype values that are not bitumen-bound; every other Massetype is an asphalt or sealed surface.
SURFACE_CATEGORIES = {
    "Grus": "Grus",
    "Betong": "Betong", "Valsebetong": "Betong", "Betongdekke, uarmert": "Betong", "Betongelementer": "Betong",
    "Belegningsstein": "Stein", "Brostein/Gatestein": "Stein",
    "Tre (bru)": "Annet", "Stålgitter (bru)": "Annet",
}
SURFACE_COLUMNS = ["surface_type", "surface_material"]
SUMMARY_PROPERTIES = {
    "traffic_aadt": ("traffic", 4623),
    "traffic_long_vehicles_pct": ("traffic", 4624),
    "traffic_year": ("traffic", 4621),
    "speed_limit_kmh": ("speed_limit", 2021),
    "calculated_road_width_m": ("calculated_road_width", 9797),
    "lane_count": ("lanes", 12629),
    "functional_road_class": ("road_class", 9338),
    "cycle_lane_count": ("lanes", 12631),
    "tunnel_length_m": ("tunnel", 1317),
    "tunnel_width_m": ("tunnel", 3897),
    "bridge_length_m": ("bridge", 1313),
    "bridge_width_m": ("bridge", 13015),
}
TEXT_PROPERTIES = {
    "tunnel_names": ("tunnel", 1081),
    "bridge_names": ("bridge", 1080),
    "accident_dates": ("accident", 5055),
}
DATASET_COLUMNS = [
    "traffic_count", "speed_limit_count", "speed_limit_object_ids", "tunnel_count", "bridge_count",
    "accident_count", "cattle_grid_count", "speed_bump_count", "speed_bump_object_ids",
]
NVDB_COLUMNS = ["nvdb_reference", "nvdb_lookup_status", *SUMMARY_PROPERTIES, *TEXT_PROPERTIES, *DATASET_COLUMNS,
                *SURFACE_COLUMNS]
NVDB_NUMERIC = {
    "traffic_aadt", "traffic_long_vehicles_pct", "traffic_year", "speed_limit_kmh",
    "calculated_road_width_m", "lane_count", "functional_road_class",
    "cycle_lane_count", "tunnel_length_m", "tunnel_width_m", "bridge_length_m", "bridge_width_m",
    *(column for column in DATASET_COLUMNS if column.endswith("_count")),
}
REFERENCE_PATTERN = re.compile(r"(.+) m(\d+)")


def normalize_municipality_number(value: str | int | float) -> str:
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,4}(?:\.0+)?", text):
        raise ValueError(f"Invalid municipality number: {value!r}. Expected an integer code.")
    number = int(text.split(".", 1)[0])
    if not 1 <= number <= 9999:
        raise ValueError(f"Invalid municipality number: {value!r}. Expected a code from 0001 to 9999.")
    return f"{number:04d}"


def lookup_objects(session: requests.Session, object_type: int, reference: str) -> list[dict]:
    params = {
        "vegsystemreferanse": reference,
        "inkluder": "egenskaper",
        "antall": 100,
    }
    objects = []
    seen_starts = set()
    while True:
        response = session.get(
            f"{OBJECT_URL}/{object_type}", params=params, headers=HEADERS, timeout=20,
        )
        if response.status_code == 204:
            break
        response.raise_for_status()
        data = response.json()
        if not data["objekter"]:
            break
        objects.extend(data["objekter"])
        next_start = data.get("metadata", {}).get("neste", {}).get("start")
        if not next_start:
            break
        if next_start in seen_starts:
            raise ValueError(f"NVDB pagination repeated for object type {object_type}.")
        seen_starts.add(next_start)
        params["start"] = next_start
    return list({record["id"]: record for record in objects}.values())


def summarize_surface(records: list[dict]) -> dict:
    """Describe the top (wearing) layer; roads without a Vegdekke object, e.g. most private roads, are Ukjent."""
    layers = [{item["id"]: item.get("verdi") for item in record.get("egenskaper", [])} for record in records]
    top = [layer for layer in layers if layer.get(SURFACE_LAYER) != "Bindlag"] or layers
    materials = sorted({str(layer[SURFACE_MATERIAL]) for layer in top if layer.get(SURFACE_MATERIAL)})
    types = sorted({SURFACE_CATEGORIES.get(material, "Asfalt") for material in materials})
    return {"surface_type": "; ".join(types) or "Ukjent", "surface_material": "; ".join(materials) or None}


def summarize_records(datasets: dict[str, list[dict]]) -> dict:
    result = {}
    if "surface" in datasets:
        result.update(summarize_surface(datasets["surface"]))
    for column in DATASET_COLUMNS:
        if column.endswith("_count"):
            records = datasets.get(column.removesuffix("_count"))
            if records is not None:
                result[column] = len(records)
        else:
            records = datasets.get(column.removesuffix("_object_ids"))
            if records is not None:
                result[column] = "; ".join(str(record["id"]) for record in records)
    for column, (name, property_id) in SUMMARY_PROPERTIES.items():
        if name not in datasets:
            continue
        records = datasets[name]
        value = None
        # Never combine attributes from different lanes, objects or traffic years.
        if len(records) == 1:
            properties = {item["id"]: item.get("verdi") for item in records[0].get("egenskaper", [])}
            value = properties.get(property_id)
            if value is not None and column in NVDB_NUMERIC:
                value = float(str(value).replace(",", "."))
                if not math.isfinite(value):
                    raise ValueError(f"NVDB {column} is not finite.")
        result[column] = value
    for column, (name, property_id) in TEXT_PROPERTIES.items():
        if name in datasets:
            values = {
                str(item["verdi"]) for record in datasets[name] for item in record.get("egenskaper", [])
                if item["id"] == property_id and item.get("verdi") not in (None, "")
            }
            result[column] = "; ".join(sorted(values)) or None
    return result


def lookup_route_data(session: requests.Session, kortform: str | None, kommunenr: str | int | float | None) -> dict:
    result = dict.fromkeys(NVDB_COLUMNS)
    if not kortform or not kommunenr:
        result["nvdb_lookup_status"] = "no_road_reference"
        return result
    municipality = normalize_municipality_number(kommunenr)
    reference = f"{municipality} {kortform}"
    result["nvdb_reference"] = reference
    match = REFERENCE_PATTERN.fullmatch(kortform)
    if match is None:
        result["nvdb_lookup_status"] = "reference_without_metres"
        return result
    prefix, metre = match.group(1), int(match.group(2))
    nearby = f"{municipality} {prefix} m{max(0, metre - NEARBY_METRES)}-{metre + NEARBY_METRES}"
    datasets = {}
    for name, object_type, use_interval in DATASETS:
        records = lookup_objects(session, object_type, nearby if use_interval else reference)
        datasets[name] = records
    result.update(summarize_records(datasets))
    result["nvdb_lookup_status"] = "complete"
    return result
