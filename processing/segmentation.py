"""Suggest sections while preserving categorized climbs and descents."""

from collections import Counter

import numpy as np
import pandas as pd

from .elev_profile import elevation_segments


def suggest_segments(
    profile: pd.DataFrame,
    extrema: pd.DataFrame,
    route: pd.DataFrame,
) -> pd.DataFrame:
    """Protect HC/Cat 1/Cat 2 sections, then add administrative and long private-road boundaries.

    Categorize full consecutive-extrema sections before introducing other boundaries. No boundary may split
    an HC, Cat 1 or Cat 2 section. Lower categories and other extrema do not add elevation boundaries.
    Elsewhere, retain fylke/municipality borders and ends of continuous P/S stretches
    strictly longer than 2 km. Edge type follows its destination point, matching private-road map highlighting.
    Administrative boundaries use the first point with a new known value. Unknown values do not create borders.
    Labels use smoothed lookup attributes within each half-open section (the last includes the endpoint).
    Names prioritise fully private sections, then protected climbs, then road and municipality.
    """
    prepared = _prepare(profile, route)
    route, orders, distances, private_edges = prepared
    turning = extrema.sort_values("route_order")
    if not turning["kind"].isin(["peak", "valley"]).all():
        raise ValueError("Elevation boundaries must be classified as peak or valley.")
    full_sections = elevation_segments(profile, turning)
    categorized = full_sections.loc[full_sections["strava_equivalent_category"].isin(["HC", "Cat 1", "Cat 2"])]
    protected = list(zip(categorized["start_route_order"], categorized["end_route_order"]))
    reasons = {float(orders[0]): "start", float(orders[-1]): "finish"}

    def inside_categorized(order: float) -> bool:
        return any(start < order < end for start, end in protected)

    def add_boundary(order: float, reason: str) -> None:
        if inside_categorized(order):
            return
        if order in reasons:
            reasons[order] += f" / {reason}"
        else:
            reasons[order] = reason

    for start, end, category, direction in zip(
        categorized["start_route_order"].to_numpy(dtype=float),
        categorized["end_route_order"].to_numpy(dtype=float),
        categorized["strava_equivalent_category"].to_numpy(dtype=str),
        categorized["direction"].to_numpy(dtype=str),
    ):
        label = f"{category} {direction.lower()}"
        add_boundary(float(start), f"{label} start")
        add_boundary(float(end), f"{label} end")

    for column, reason in (("fylke", "fylke border"), ("kommunename", "municipality border")):
        values = route[column].astype("string").str.strip()
        if column == "kommunename" and "kommunenr" in route:
            codes = route["kommunenr"].astype("string").str.strip()
            values = codes.mask(codes.eq("")).fillna(values)
        values = values.mask(values.eq(""))
        borders = values.notna() & values.shift().notna() & values.ne(values.shift()).fillna(False)
        for order in route.loc[borders, "route_order"].to_numpy(dtype=float):
            add_boundary(float(order), reason)

    transitions = np.diff(np.concatenate(([False], private_edges, [False])).astype(int))
    for start, end in zip(np.flatnonzero(transitions == 1), np.flatnonzero(transitions == -1)):
        if distances[end] - distances[start] > 2.0:
            add_boundary(float(orders[start]), "private stretch start (>2 km)")
            add_boundary(float(orders[end]), "private stretch end (>2 km)")

    boundaries = pd.DataFrame([
        {"route_order": order, "kind": reason}
        for order, reason in sorted(reasons.items()) if orders[0] < order < orders[-1]
    ], columns=["route_order", "kind"])
    climbs = set(zip(
        categorized.loc[categorized["direction"].eq("Uphill"), "start_route_order"],
        categorized.loc[categorized["direction"].eq("Uphill"), "end_route_order"],
    ))
    return _describe(profile, prepared, boundaries, climbs)


def segment_details(profile: pd.DataFrame, route: pd.DataFrame, segments: pd.DataFrame) -> pd.DataFrame:
    """Rebuild measurements and lookup labels for saved segments from their start points and the route.

    Saved names, descriptions and boundary reasons are kept.
    """
    if segments.empty:
        return segments
    saved = segments.sort_values("segment").reset_index(drop=True)
    starts = saved["start_route_order"].to_numpy(dtype=float)
    if starts[0] != float(profile["route_order"].min()):
        raise ValueError("Saved segments do not match the route. Generate the segments again.")
    boundaries = pd.DataFrame({"route_order": starts[1:], "kind": saved["from"].iloc[1:].to_numpy()})
    details = _describe(profile, _prepare(profile, route), boundaries, set())
    labels = ["segment", "segment_name", "from", "to"]
    details[labels] = saved[labels].to_numpy()
    details.insert(2, "segment_desc", saved["segment_desc"].to_numpy())
    return details


def _prepare(profile: pd.DataFrame, route: pd.DataFrame):
    route = route.sort_values("route_order").reset_index(drop=True)
    if route["route_order"].duplicated().any() or not route["route_order"].isin(profile["route_order"]).all():
        raise ValueError("Elevation profile and lookup route points must match. Complete lookup and refit elevation.")
    # Excluded points retain their GPX geometry but do not contribute road attributes.
    route = route.set_index("route_order").reindex(profile["route_order"]).reset_index()
    orders = profile["route_order"].to_numpy(dtype=float)
    distances = profile["distance_cum_km"].to_numpy(dtype=float)
    if len(orders) < 2:
        raise ValueError("At least two route points are required for segmentation.")
    private = route["smoothed_vei_id"].astype("string").str.startswith(("P", "S")).fillna(False)
    return route, orders, distances, private.to_numpy(dtype=bool)[1:]


def _describe(profile: pd.DataFrame, prepared, boundaries: pd.DataFrame, climbs: set) -> pd.DataFrame:
    """Measure the sections between boundaries and add names, private distance and lookup labels."""
    route, orders, distances, private_edges = prepared
    segments = elevation_segments(profile, boundaries)
    label_columns = ("fylke", "kommunename", "politidistrikt", "roadname")
    labels = {column: [] for column in label_columns}
    segment_names = []
    private_distances = []
    for index, (start, end) in enumerate(zip(
        segments["start_route_order"].to_numpy(dtype=float), segments["end_route_order"].to_numpy(dtype=float)
    )):
        overlap = np.maximum(
            0, np.minimum(orders[1:], end) - np.maximum(orders[:-1], start))
        edge_distances = np.diff(distances) * overlap / np.diff(orders)
        private_distance = float(edge_distances[private_edges].sum())
        private_distances.append(private_distance)
        is_private = private_distance > 0 and not (edge_distances[~private_edges] > 0).any()

        stop_side = "right" if index == len(segments) - 1 else "left"
        points = route.iloc[np.searchsorted(orders, start):np.searchsorted(orders, end, side=stop_side)]
        counts = {}
        for column in label_columns:
            values = points[column].astype("string").str.strip().dropna()
            counts[column] = Counter(str(value) for value in values if value)
            labels[column].append(", ".join(counts[column]) or "Unknown")

        road_counts = counts["roadname"]
        if is_private:
            # Private stretches are defined by the road ID at each edge's destination.
            road_names = route["roadname"].iloc[np.flatnonzero(edge_distances > 0) + 1]
            values = road_names.astype("string").str.strip().dropna()
            road_counts = Counter(str(value) for value in values if value)
        road_name = next(iter(road_counts.most_common(1)), ("Unnamed road", 0))[0]
        if is_private:
            segment_names.append(f"{road_name} (Private)")
        elif (start, end) in climbs:
            segment_names.append(f"{road_name} Climb")
        else:
            municipality = next(iter(counts["kommunename"].most_common(1)), ("Unknown municipality", 0))[0]
            segment_names.append(f"{road_name} ({municipality})")

    segments.insert(1, "segment_name", segment_names)
    segments["private_distance_km"] = private_distances
    for column, names in labels.items():
        segments[column] = names
    return segments
