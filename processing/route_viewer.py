"""Pydeck route map (moved from the old bikingman viewer)."""

import json
import math
from urllib.parse import quote

import pydeck as pdk

PRIVATE_ROAD_COLOR = list(bytes.fromhex("f2c94c"))
SV_ROAD_COLOR = list(bytes.fromhex("ee922c"))
ROUTE_COLOR = [23, 107, 114]
BASEMAP_STYLE = "data:application/json;charset=utf-8," + quote(json.dumps({
    "version": 8,
    "sources": {"topographic": {
        "type": "raster",
        "tiles": ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}"],
        "tileSize": 256,
        "attribution": "Tiles &copy; Esri, HERE, Garmin, USGS, NGA and the GIS User Community",
    }},
    "layers": [{"id": "topographic", "type": "raster", "source": "topographic"}],
}))


def route_map(route, pois, basemap, *, colored_paths=None):
    """Route as one line, or as `colored_paths` ({"path": [[lon, lat], ...], "color": [r, g, b]}), with POI markers."""
    sections = route.groupby("_route_path", sort=False) if "_route_path" in route else [(0, route)]
    paths = [
        {"path": section[["lon", "lat"]].to_numpy().tolist()}
        for _, section in sections if len(section) > 1
    ] if colored_paths is None else []
    markers = pois.copy()
    markers["position"] = markers[["lon", "lat"]].to_numpy().tolist()
    markers["label"] = markers["name"]
    positions = markers[["lon", "lat"]].to_numpy().tolist()
    markers["label_offset"] = [
        [10, -12 - 18 * sum(
            (lat - previous_lat) ** 2 + ((lon - previous_lon) * math.cos(math.radians(lat))) ** 2 < 0.35 ** 2
            for previous_lon, previous_lat in positions[:index]
        )] for index, (lon, lat) in enumerate(positions)
    ]
    markers["distance"] = markers["distance_cum_km"].map("{:.1f}".format)
    markers["elevation"] = markers["elevation_m"].map("{:.0f}".format)
    if colored_paths is not None:
        route_layer = pdk.Layer("PathLayer", data=colored_paths, get_path="path",
                                get_color="color", get_width=4, width_min_pixels=3, width_max_pixels=3)
    else:
        route_layer = pdk.Layer("PathLayer", data=paths, get_path="path",
                                get_color=ROUTE_COLOR, get_width=4, width_min_pixels=3, width_max_pixels=3)
    layers = [
        route_layer,
        pdk.Layer("ScatterplotLayer", data=markers, get_position="position",
                  get_fill_color=[196, 71, 50], get_radius=6, radius_min_pixels=5, radius_max_pixels=5,
                  get_line_color=[255, 255, 255], stroked=True, line_width_min_pixels=1, pickable=True),
        pdk.Layer("TextLayer", data=markers, get_position="position", get_text="label",
                  get_color=[35, 35, 35], get_size=13, get_pixel_offset="label_offset",
                  character_set="'auto'",
                  background=True, get_background_color=[255, 255, 255, 210], background_padding=[2, 2],
                  get_text_anchor="'start'", get_alignment_baseline="'bottom'"),
    ]
    west, east = float(route.lon.min()), float(route.lon.max())
    south, north = float(route.lat.min()), float(route.lat.max())
    mercator_south = math.log(math.tan(math.pi / 4 + math.radians(south) / 2))
    mercator_north = math.log(math.tan(math.pi / 4 + math.radians(north) / 2))
    span_x = max((east - west) / 360, 1e-8)
    span_y = max((mercator_north - mercator_south) / (2 * math.pi), 1e-8)
    zoom = min(math.log2(340 * 0.8 / (512 * span_x)), math.log2(530 * 0.8 / (512 * span_y)))
    center_lat = math.degrees(2 * math.atan(math.exp((mercator_south + mercator_north) / 2)) - math.pi / 2)
    view = pdk.ViewState(latitude=center_lat, longitude=(west + east) / 2, zoom=max(0, min(16, zoom)), pitch=0)
    return pdk.Deck(layers=layers, initial_view_state=view, map_style=BASEMAP_STYLE if basemap else None,
                    map_provider="mapbox", tooltip={"text": "{name}\n{distance} km | {elevation} m"})
