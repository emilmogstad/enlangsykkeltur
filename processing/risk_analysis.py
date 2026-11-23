"""Calculate the dashboard's original 0-5 route risk scores."""

import numpy as np
import pandas as pd

from .data_handler import RISK_COLUMNS, SEGMENT_RISK_COLUMNS

DESCENT_RISK = {"HC": 5, "Cat 1": 4, "Cat 2": 3, "Cat 3": 2, "Cat 4": 1}


def descent_risk(route_order: pd.Series, sections: pd.DataFrame | None) -> np.ndarray:
    if sections is None:
        return np.full(len(route_order), np.nan)

    levels = np.zeros(len(route_order))
    if sections.empty:
        return levels

    sections = sections.sort_values("start_route_order")
    starts = sections["start_route_order"].to_numpy(dtype=float)
    ends = sections["end_route_order"].to_numpy(dtype=float)
    orders = route_order.to_numpy(dtype=float)
    # At a shared boundary use the new section; include the final route point.
    indices = np.searchsorted(starts, orders, side="right") - 1
    valid = (indices >= 0) & (orders <= ends[indices])
    section_levels = sections["strava_equivalent_category"].map(DESCENT_RISK).fillna(0)
    section_levels = section_levels.where(sections["direction"].eq("Downhill"), 0).to_numpy(dtype=float)
    levels[valid] = section_levels[indices[valid]]
    return levels


def calculate_risks(route: pd.DataFrame, elevation_sections: pd.DataFrame | None) -> pd.DataFrame:
    """Score the full, ordered route with its effective (smoothed when available) road data."""
    distance = route["distance_cum_km"]
    elevation = route["elevation_m"]
    if route.empty or not np.isfinite(distance.to_numpy(dtype=float)).all() or not distance.is_monotonic_increasing:
        raise ValueError("Risk analysis requires finite, nondecreasing cumulative distances.")
    if not np.isfinite(elevation.to_numpy(dtype=float)).all():
        raise ValueError("Risk analysis requires finite GPX elevations.")

    traffic_inputs = route[["traffic_aadt", "traffic_long_vehicles_pct", "speed_limit_kmh"]].apply(
        pd.to_numeric, errors="coerce")
    traffic_inputs = traffic_inputs.where(np.isfinite(traffic_inputs) & traffic_inputs.ge(0))
    aadt = traffic_inputs["traffic_aadt"]
    factors = traffic_inputs.div([10_000, 20, 80]).clip(upper=5)

    missing = aadt.isna()
    runs = distance.groupby(missing.ne(missing.shift(fill_value=False)).cumsum(), sort=False)
    missing_km = runs.transform("max") - runs.transform("min")
    missing_risk = (2 + 3 * (missing_km - 2) / 8).clip(upper=5).where(missing_km.gt(2), 0)

    scores = route[["route_order", "distance_cum_km", "_route_path"]].copy()
    scores["traffic_risk"] = (2 * factors.sum(axis=1, skipna=False)).clip(upper=5).fillna(0)
    scores["environmental_risk"] = (5 * (elevation - 200) / 800).clip(0, 5)
    scores["crash_risk"] = descent_risk(route["route_order"], elevation_sections)
    scores["fatigue_risk"] = (5 * (distance - distance.iloc[0]) / 300).clip(0, 5)
    scores["remoteness_risk"] = (5 * (400 - aadt) / 400).clip(0, 5).where(~missing, missing_risk)
    scores["aggregate_risk"] = scores[RISK_COLUMNS[:-1]].mean(axis=1, skipna=False)
    return scores


def summarize_segment_risks(segments: pd.DataFrame, scores: pd.DataFrame) -> pd.DataFrame:
    """Summarize observed scores; ties for the aggregate peak go to the earliest point."""
    scores = scores.sort_values("route_order").reset_index(drop=True)
    orders = scores["route_order"].to_numpy(dtype=float)
    start = segments["start_route_order"].to_numpy(dtype=float)
    end = segments["end_route_order"].to_numpy(dtype=float)
    left = np.searchsorted(orders, start)
    right = np.searchsorted(orders, end)
    # Shared boundary points belong to the next segment, as in the segmentation table.
    if len(end):
        final = end == end.max()
        right[final] = np.searchsorted(orders, end[final], side="right")
    components = RISK_COLUMNS[:-1]
    records = []
    for a, b in zip(left, right):
        window = scores.iloc[a:b]
        aggregate = window["aggregate_risk"]
        peak = window.loc[aggregate.idxmax()] if aggregate.notna().any() else None
        records.append({"total_risk": aggregate.max(),
                        "risk_peak_route_order": peak["route_order"] if peak is not None else None,
                        **{f"max_{c}": window[c].max() for c in components},
                        **{f"peak_{c}": peak[c] if peak is not None else None for c in components}})
    return pd.DataFrame(records, index=segments.index, columns=SEGMENT_RISK_COLUMNS)
