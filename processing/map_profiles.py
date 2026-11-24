"""Build matching map paths and elevation lines from the visible route."""

import altair as alt
import numpy as np
import pandas as pd

from .processing import viewer

ROUTE_COLOR = "#176b72"
SEGMENT_COLORS = ("#0072b2", "#d55e00", "#009e73", "#984ea3", "#ca8300", "#4b526b",
                  "#cc4b89", "#397f21", "#5a55bd", "#a55837")
POINT_COLUMNS = ["lon", "lat", "distance_cum_km", "elevation_m"]
PRIVATE_COLORS = {
    key: "#" + bytes(color).hex()
    for key, color in (("P", viewer.PRIVATE_ROAD_COLOR), ("S", viewer.SV_ROAD_COLOR))
}


def colored_runs(
    route: pd.DataFrame, *, private: bool = False, segments: pd.DataFrame | None = None,
) -> list[tuple[str, np.ndarray]]:
    """Return colour and point arrays per continuous run; share transition points between runs."""
    runs = []
    if segments is not None:
        starts = segments["start_route_order"].to_numpy(dtype=float)
        colors = [SEGMENT_COLORS[(number - 1) % len(SEGMENT_COLORS)] for number in segments["segment"]]
    for _, section in route.groupby("_route_path", sort=False):
        if len(section) < 2:
            continue
        points = section[POINT_COLUMNS].to_numpy(dtype=float)
        if segments is not None:
            orders = section["route_order"].to_numpy(dtype=float)
            cuts = starts[(starts > orders[0]) & (starts < orders[-1])]
            bounds = np.concatenate(([orders[0]], cuts, [orders[-1]]))
            edges = np.column_stack([np.interp(bounds, orders, column) for column in points.T])
            left = np.searchsorted(orders, bounds[:-1], side="right")
            right = np.searchsorted(orders, bounds[1:], side="left")
            indices = np.searchsorted(starts, bounds[:-1], side="right") - 1
            for i, (start, end, segment_index) in enumerate(zip(left, right, indices)):
                runs.append((colors[segment_index], np.vstack((edges[i], points[start:end], edges[i + 1]))))
        elif private:
            prefixes = section["smoothed_vei_id"].iloc[1:].astype("string").str[0]
            edge_colors = prefixes.map(PRIVATE_COLORS).fillna(ROUTE_COLOR).to_numpy()
            cuts = np.flatnonzero(edge_colors[1:] != edge_colors[:-1]) + 1
            bounds = np.concatenate(([0], cuts, [len(edge_colors)]))
            for start, end in zip(bounds[:-1], bounds[1:]):
                runs.append((edge_colors[start], points[start:end + 1]))
        else:
            runs.append((ROUTE_COLOR, points))
    return runs


def map_paths(runs: list[tuple[str, np.ndarray]]) -> list[dict]:
    return [{"path": points[:, :2].tolist(), "color": list(bytes.fromhex(color[1:]))}
            for color, points in runs]


def colored_elevation_chart(runs: list[tuple[str, np.ndarray]]) -> alt.Chart:
    rows = [(distance, elevation, run_id, point_id, color)
            for run_id, (color, points) in enumerate(runs)
            for point_id, (_, _, distance, elevation) in enumerate(points)]
    frame = pd.DataFrame(rows, columns=["distance_cum_km", "elevation_m", "run", "point", "color"])
    return alt.Chart(frame).mark_line(strokeWidth=1.5).encode(
        x=alt.X("distance_cum_km:Q", title="Distance along route (km)", scale=alt.Scale(zero=False)),
        y=alt.Y("elevation_m:Q", title="Elevation (m)"),
        color=alt.Color("color:N", scale=None, legend=None),
        detail="run:N",
        order="point:Q",
        tooltip=[alt.Tooltip("distance_cum_km:Q", title="Distance (km)", format=".1f"),
                 alt.Tooltip("elevation_m:Q", title="Elevation (m)", format=".0f")],
    ).properties(height=300).interactive(bind_y=False)
