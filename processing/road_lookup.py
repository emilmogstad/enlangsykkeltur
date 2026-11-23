"""NVDB road and Kartverket property lookups (moved from the old bikingman tools)."""

import math

import requests

LOOKUP_URL = "https://nvdbapiles.atlas.vegvesen.no/vegnett/api/v4/posisjon"
ROADNAME_LOOKUP_URL = "https://nvdbapiles.atlas.vegvesen.no/vegobjekter/api/v4/vegobjekter/538"
NVDB_HEADERS = {"Accept": "application/json", "X-Client": "BikingMan-Norway-route-lookups"}
PROPERTY_LOOKUP_URL = "https://api.kartverket.no/eiendom/v1/punkt"


def haversine_m(lat1, lon1, lat2, lon2):
    """Distance in meters between two lat/lon points."""
    radius = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    haversine = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    haversine = min(1.0, max(0.0, haversine))
    return 2 * radius * math.atan2(math.sqrt(haversine), math.sqrt(1 - haversine))


def lookup_position(session: requests.Session, lat: float, lon: float):
    response = session.get(
        LOOKUP_URL, params={"lat": lat, "lon": lon}, headers=NVDB_HEADERS, timeout=20,
    )
    if response.status_code in (204, 404):
        return None
    response.raise_for_status()
    if not response.content.strip():
        return None
    positions = response.json()
    for position in sorted(positions, key=lambda item: item.get("avstand", math.inf)):
        kortform = position.get("vegsystemreferanse", {}).get("kortform", "").strip()
        municipality = position.get("kommune")
        kommunenr = str(municipality).zfill(4) if municipality is not None else ""
        if kortform and kommunenr:
            return kortform, kommunenr
    return None


def lookup_roadname(session: requests.Session, kortform: str, kommunenr: str):
    names = set()
    params = {
        "vegsystemreferanse": f"{kommunenr} {kortform}",
        "inkluder": "egenskaper",
        "antall": 100,
    }
    while True:
        response = session.get(
            ROADNAME_LOOKUP_URL, params=params, headers=NVDB_HEADERS, timeout=20,
        )
        if response.status_code in (204, 404):
            break
        response.raise_for_status()
        if not response.content.strip():
            break
        data = response.json()
        if not data["objekter"]:
            break
        for road_address in data["objekter"]:
            for property_record in road_address.get("egenskaper", []):
                if property_record.get("id") == 4589:
                    name = (property_record.get("verdi") or "").strip()
                    if name:
                        names.add(name)
        next_start = data.get("metadata", {}).get("neste", {}).get("start")
        if not next_start or next_start == params.get("start"):
            break
        params["start"] = next_start
    return "; ".join(sorted(names)) or None


def lookup_property_details(session: requests.Session, lat: float, lon: float):
    """Return property identifiers for Kartverket's zero-distance matches."""
    properties = {}
    page = 1
    while True:
        response = session.get(
            PROPERTY_LOOKUP_URL,
            params={
                "nord": lat, "ost": lon, "koordsys": 4326,
                "radius": 1, "treffPerSide": 100, "side": page,
            },
            headers={"Accept": "application/json"},
            timeout=20,
        )
        if response.status_code in (204, 404):
            break
        response.raise_for_status()
        data = response.json()
        for property_record in data["eiendom"]:
            if property_record.get("meterFraPunkt") != 0:
                continue
            municipality = property_record.get("kommunenummer")
            number = property_record.get("matrikkelnummertekst")
            if municipality and number:
                full_number = f"{municipality}-{number}"
                properties[full_number] = {
                    "matrikkelnummer": full_number,
                    "kommunenummer": municipality,
                    "bruksnummer": property_record.get("bruksnummer"),
                    "gardsnummer": property_record.get("gardsnummer"),
                }
        if page * 100 >= data["metadata"]["totaltAntallTreff"]:
            break
        page += 1
    details = {}
    for field in ("matrikkelnummer", "kommunenummer", "bruksnummer", "gardsnummer"):
        values = [property_record[field] for property_record in properties.values()]
        if len(values) > 1:
            details[field] = "; ".join(str(value) if value is not None else "" for value in values)
        else:
            details[field] = values[0] if values else None
    return details


def find_road_jump_flags(road_ids, max_jump_rows: int = 20):
    """Flag short excursions bounded by the same road, without crossing gaps."""
    if max_jump_rows < 1:
        raise ValueError("max_jump_rows must be a positive integer.")
    flags = [False] * len(road_ids)
    index = 1
    while index < len(road_ids):
        original_road = road_ids[index - 1]
        if not original_road or not road_ids[index] or road_ids[index] == original_road:
            index += 1
            continue
        for return_index in range(index + 1, min(index + max_jump_rows + 1, len(road_ids))):
            if not road_ids[return_index]:
                break
            if road_ids[return_index] == original_road:
                flags[index:return_index] = [True] * (return_index - index)
                index = return_index
                break
        index += 1
    return flags
