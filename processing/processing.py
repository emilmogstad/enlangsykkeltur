"""Experimental route smoothing with preserved original lookup data and saved donor points.

Run from the "01 Dashboard" folder with: python -m processing.smoothing [race_id] [aic|bic]
"""

import numpy as np
import pandas as pd

from .data_handler import data_handler

EARTH_RADIUS_M = 6_371_000
DEFAULT_RACE_ID = 'a03ff1d1-4546-4ab3-8c6b-63073816b190'


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres. Accepts single numbers or numpy arrays/pandas Series."""
    phi1, phi2 = np.radians(lat1), np.radians(lat2)
    delta_phi = phi2 - phi1
    delta_lambda = np.radians(lon2) - np.radians(lon1)
    a = np.sin(delta_phi / 2) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(delta_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(a))


def step_distances_m(route: pd.DataFrame) -> np.ndarray:
    """Distance from the previous point to each point (0 for the first point)."""
    lat, lon = route["lat"].to_numpy(), route["lon"].to_numpy()
    return np.concatenate([[0.0], haversine_m(lat[:-1], lon[:-1], lat[1:], lon[1:])])


def load_route(race_id: str, db: data_handler | None = None) -> pd.DataFrame:
    """All original lookup and GPX point fields in route order."""
    db = db or data_handler()
    return db.lookup_route(race_id, smoothed=False)


def get_count(route: pd.DataFrame) -> pd.DataFrame:
    """Each vei_id with the number of route points on it, most frequent first. Points without a vei_id are skipped."""
    return route["vei_id"].value_counts().rename_axis("vei_id").reset_index(name="count")


def distance_to_segment_m(lat, lon, lat_a, lon_a, lat_b, lon_b):
    """Shortest distance in metres from points P to the straight segments A-B (arrays of equal length).

    Uses a flat projection centred on each P, which is accurate for the few-kilometre gaps smoothing deals with.
    """
    lat0 = np.radians(lat)
    scale_x = EARTH_RADIUS_M * np.cos(lat0)
    ax, ay = np.radians(lon_a - lon) * scale_x, np.radians(lat_a - lat) * EARTH_RADIUS_M
    bx, by = np.radians(lon_b - lon) * scale_x, np.radians(lat_b - lat) * EARTH_RADIUS_M
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    with np.errstate(invalid="ignore", divide="ignore"):
        t = np.where(length2 > 0, -(ax * dx + ay * dy) / length2, 0.0)
    t = np.clip(t, 0.0, 1.0)
    return np.hypot(ax + t * dx, ay + t * dy)


def distance_to_road(route: pd.DataFrame, vei_id: str) -> pd.DataFrame:
    """For every route point, the distance to one road measured against the line between the road's nearest
    other points before and after it along the route.

    A point is never compared with itself, so a point on the road is measured against the line between the
    road's previous and next points. With only one neighbour, the distance to that point is used; with none
    (e.g. the point is the road's only point, or the road does not occur in the route) the distance is NaN.
    """
    n = len(route)
    on_road = np.flatnonzero(route["vei_id"].eq(vei_id).to_numpy())
    result = pd.DataFrame({"distance_m": np.nan, "before_route_order": pd.NA, "after_route_order": pd.NA},
                          index=route.index)
    if not len(on_road):
        return result
    positions = np.arange(n)
    before = np.searchsorted(on_road, positions, side="left") - 1
    after = np.searchsorted(on_road, positions, side="right")
    has_before, has_after = before >= 0, after < len(on_road)
    before_pos = np.where(has_before, on_road[np.clip(before, 0, None)], on_road[np.clip(after, None, len(on_road) - 1)])
    after_pos = np.where(has_after, on_road[np.clip(after, None, len(on_road) - 1)], before_pos)
    lat, lon = route["lat"].to_numpy(), route["lon"].to_numpy()
    distance = distance_to_segment_m(lat, lon, lat[before_pos], lon[before_pos], lat[after_pos], lon[after_pos])
    result["distance_m"] = np.where(has_before | has_after, distance, np.nan)
    orders = route["route_order"].to_numpy()
    result["before_route_order"] = pd.Series(orders[before_pos], index=route.index, dtype="Int64").where(has_before)
    result["after_route_order"] = pd.Series(orders[after_pos], index=route.index, dtype="Int64").where(has_after)
    return result


def distance_to_roads(route: pd.DataFrame, vei_ids: list[str]) -> pd.DataFrame:
    """For every route point, the distance to the closest of the given vei_ids, using distance_to_road.

    Columns: route_order of the point, distance_m to the closest road, closest_vei_id, and
    before_route_order/after_route_order of the two road points the line was drawn between.
    If none of the vei_ids occur in the route, the distance and closest columns are empty.
    """
    per_road = [distance_to_road(route, vei_id) for vei_id in vei_ids]
    result = pd.DataFrame({
        "route_order": route["route_order"],
        "distance_m": np.nan,
        "closest_vei_id": pd.Series(pd.NA, index=route.index, dtype="object"),
        "before_route_order": pd.Series(pd.NA, index=route.index, dtype="Int64"),
        "after_route_order": pd.Series(pd.NA, index=route.index, dtype="Int64"),
    }, index=route.index)
    if not per_road:
        return result
    distances = np.column_stack([frame["distance_m"].to_numpy(dtype=float) for frame in per_road])
    found = ~np.isnan(distances).all(axis=1)
    best = np.zeros(len(route), dtype=int)
    best[found] = np.nanargmin(distances[found], axis=1)
    rows = np.arange(len(route))
    result.loc[found, "distance_m"] = distances[rows, best][found]
    result.loc[found, "closest_vei_id"] = np.asarray(vei_ids, dtype=object)[best][found]
    for column in ("before_route_order", "after_route_order"):
        values = pd.concat([frame[column] for frame in per_road], axis=1).to_numpy(dtype=object)
        result.loc[found, column] = pd.array(values[rows, best][found], dtype="Int64")
    return result

def _criterion(distances, k: int, penalty_per_parameter) -> float:
    """-2·logL of a regression with normally distributed errors (distances as residuals) plus k times the penalty.

    penalty_per_parameter is called with n, the number of residuals. Returns -inf for a perfect fit or no points.
    """
    d = np.asarray(distances, dtype=float)
    d = d[~np.isnan(d)]
    n = len(d)
    if n == 0:
        return -np.inf
    sigma2 = np.dot(d, d) / n
    if sigma2 == 0:
        return -np.inf
    log_likelihood = -n / 2 * (np.log(2 * np.pi * sigma2) + 1)
    return k * penalty_per_parameter(n) - 2 * log_likelihood


def aic(distances, k: int) -> float:
    """AIC = 2k - 2·logL, treating distances as the residuals and k as the number of parameters."""
    return _criterion(distances, k, lambda n: 2)


def bic(distances, k: int) -> float:
    """BIC = k·ln(n) - 2·logL, treating distances as the residuals and k as the number of parameters."""
    return _criterion(distances, k, np.log)


CRITERIA = {"aic": aic, "bic": bic}


def iterative_aic(route: pd.DataFrame, min_points: int = 100, criterion: str = "bic") -> pd.DataFrame:
    """Select roads by AIC or BIC (criterion="aic"/"bic"). BIC excludes roads with fewer than three points.
    Eligible roads with at least min_points points are always included; the others are tried most frequent first
    and kept only if they lower the criterion.

    The criterion only counts points that are not on a base road. After selection, points on any selected road
    keep their original road and get a smoothing distance of zero.

    Returns the original route with smoothed_vei_id, smoothing_distance_m and smoothed_source_route_order.
    For reassigned points, the donor is the nearer endpoint of the chosen road's segment.
    """
    score, label = CRITERIA[criterion], criterion.upper()
    original = route
    # Junction parts (kortform ending in "KD<n> m<n>") are always smoothed away: they belong to no road, so
    # they never count towards a road or serve as donors, and get the nearest selected road.
    junction = route["kortform"].astype("string").str.contains(r"\bKD\d+ m\d+$", na=False)
    route = route.assign(vei_id=route["vei_id"].mask(junction))
    counts = get_count(route)
    eligible = counts.loc[counts["count"] >= 3] if criterion == "bic" else counts
    selected = eligible.loc[eligible["count"] >= min_points, "vei_id"].tolist()
    remaining = eligible.loc[eligible["count"] < min_points, "vei_id"].tolist()
    off_base = ~route["vei_id"].isin(selected).to_numpy()
    print(f"{off_base.sum()} of {len(route)} points are not on a base road (>= {min_points} points); "
          f"{junction.sum()} junction points are always smoothed")
    best_score, best_nearest = np.inf, None
    if selected:
        best_nearest = distance_to_roads(route, selected)
        best_score = score(best_nearest["distance_m"].to_numpy()[off_base], len(selected))
        print(f"{label} with {len(selected)} base roads: {best_score:,.1f}")
    for road in remaining:
        if np.isneginf(best_score):
            break
        candidate = selected + [road]
        nearest = distance_to_roads(route, candidate)
        candidate_score = score(nearest["distance_m"].to_numpy()[off_base], len(candidate))
        if candidate_score < best_score:
            print(f"{label} with {len(candidate)} roads (+{road}): {candidate_score:,.1f} - kept")
            best_score, best_nearest, selected = candidate_score, nearest, candidate
        else:
            print(f"{label} with {len(candidate)} roads (+{road}): {candidate_score:,.1f} - skipped")
    roads = counts["vei_id"].tolist()
    print(f"Selected {len(selected)} of {len(roads)} roads, {label} {best_score:,.1f}: {selected}")
    smoothed = original.copy()
    smoothed["smoothed_source_route_order"] = pd.Series(pd.NA, index=route.index, dtype="Int64")
    if best_nearest is None:
        smoothed["smoothed_vei_id"] = route["vei_id"]
        smoothed["smoothing_distance_m"] = np.nan
    else:
        smoothed["smoothed_vei_id"] = best_nearest["closest_vei_id"]
        smoothed["smoothing_distance_m"] = best_nearest["distance_m"]
        assigned = best_nearest["closest_vei_id"].notna()
        if route["route_order"].duplicated().any():
            raise ValueError("Route point orders must be unique to select smoothing donors.")
        endpoints = route.set_index("route_order")
        before = best_nearest["before_route_order"]
        after = best_nearest["after_route_order"]
        before_orders = before.fillna(after)
        after_orders = after.fillna(before)
        if assigned.any():
            points = route.loc[assigned]
            before_points = endpoints.loc[before_orders.loc[assigned].tolist()]
            after_points = endpoints.loc[after_orders.loc[assigned].tolist()]
            before_distance = haversine_m(
                points["lat"].to_numpy(), points["lon"].to_numpy(),
                before_points["lat"].to_numpy(), before_points["lon"].to_numpy())
            after_distance = haversine_m(
                points["lat"].to_numpy(), points["lon"].to_numpy(),
                after_points["lat"].to_numpy(), after_points["lon"].to_numpy())
            smoothed.loc[assigned, "smoothed_source_route_order"] = pd.array(
                np.where(before_distance <= after_distance,
                         before_orders.loc[assigned].to_numpy(),
                         after_orders.loc[assigned].to_numpy()), dtype="Int64")
    on_selected = route["vei_id"].isin(selected)
    smoothed.loc[on_selected, "smoothed_vei_id"] = route.loc[on_selected, "vei_id"]
    smoothed.loc[on_selected, "smoothing_distance_m"] = 0.0
    smoothed.loc[on_selected, "smoothed_source_route_order"] = route.loc[on_selected, "route_order"].to_numpy()
    return remove_single_points(smoothed)


def remove_single_points(smoothed: pd.DataFrame) -> pd.DataFrame:
    """Give points whose smoothed road differs from both neighbours the road and donor of the point before.

    Runs forwards in route order using the already corrected previous point, so a chain of singles all inherit
    the road before the chain. A different road at the last point also counts as a single.
    """
    ordered = smoothed.sort_values("route_order")
    roads = ordered["smoothed_vei_id"].astype(object).where(ordered["smoothed_vei_id"].notna(), None).tolist()
    donors = ordered["smoothed_source_route_order"].tolist()
    changed = []
    for i in range(1, len(roads)):
        if roads[i] != roads[i - 1] and (i + 1 == len(roads) or roads[i] != roads[i + 1]):
            roads[i], donors[i] = roads[i - 1], donors[i - 1]
            changed.append(i)
    if changed:
        index = ordered.index[changed]
        smoothed.loc[index, "smoothed_vei_id"] = [roads[i] for i in changed]
        smoothed.loc[index, "smoothed_source_route_order"] = pd.array([donors[i] for i in changed], dtype="Int64")
    print(f"{len(changed)} single points inherited the road before them")
    return smoothed


def smooth_race(db: data_handler, race_id: str, criterion: str = "bic") -> pd.DataFrame:
    """Run AIC/BIC smoothing and store the selected roads and donor points."""
    revision = db.race(race_id)["revision"]
    smoothed = iterative_aic(load_route(race_id, db), criterion=criterion)
    db.save_smoothed_vei_id(race_id, smoothed, revision=revision)
    return smoothed


if __name__ == "__main__":
    import sys

    race_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_RACE_ID
    criterion = sys.argv[2] if len(sys.argv) > 2 else "bic"
    db = data_handler()
    smoothed = smooth_race(db, race_id, criterion)
    changed = smoothed["vei_id"].ne(smoothed["smoothed_vei_id"])
    print(f"{changed.sum()} of {len(smoothed)} points get a different road")
    print(smoothed.loc[changed, ["route_order", "vei_id", "smoothed_vei_id", "smoothing_distance_m"]].head(20))
    print(f"Saved smoothed_vei_id for {len(smoothed)} points of race {race_id}")
