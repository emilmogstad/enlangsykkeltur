"""Load original route elevations as a starting point for profile smoothing.

Run from the dashboard folder: python -m processing.elev_profile
"""

from collections.abc import Callable

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.figure import Figure

from .data_handler import data_handler
from .smoothing import step_distances_m



def load_elevation_profile(race_id: str, db: data_handler | None = None) -> pd.DataFrame:
    """Return original GPX route_order, elevation_m and cumulative horizontal distance in kilometres."""
    db = db or data_handler()
    points = db.route_points(race_id)
    if points.empty:
        raise ValueError(f"No route points stored for race {race_id}. Upload a GPX first.")
    profile = points[["route_order", "elevation_m"]].copy()
    profile["distance_cum_km"] = np.cumsum(step_distances_m(points)) / 1000
    return profile


def load_saved_elevation_fit(
    db: data_handler, race_id: str, gpx_hash: str | None, profile: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame] | None:
    """Reconstruct the fitted profile, extrema and segments without refitting."""
    saved = db.load_elevation_fit(race_id, gpx_hash)
    if saved is None:
        return None
    coefficients, extrema, heights = saved
    if profile is None:
        profile = load_elevation_profile(race_id, db)
        if db.race(race_id)["gpx_hash"] != gpx_hash:
            raise ValueError("The GPX changed while loading the fit. Reload the page.")
    fitted = profile.merge(heights, on="route_order", how="left", validate="one_to_one")
    if fitted["fitted_elevation_m"].isna().any():
        raise ValueError("The saved elevation profile does not match the GPX.")
    fitted.attrs["cosine_coefficients"] = np.asarray(coefficients)
    fitted.attrs["k"] = len(coefficients) - 1
    return fitted, extrema, elevation_segments(profile, extrema)


def plot_elevation_profile(profile: pd.DataFrame, extrema: pd.DataFrame | None = None) -> Figure:
    """Plot elevations and optional fitted extrema; call plt.show() to display."""
    figure, axes = plt.subplots(figsize=(12, 4))
    axes.plot(profile["route_order"], profile["elevation_m"], label="Original")
    if "fitted_elevation_m" in profile:
        axes.plot(profile["route_order"], profile["fitted_elevation_m"], label="Cosine fit")
    if extrema is not None:
        for kind, marker, color in (("peak", "^", "red"), ("valley", "v", "green")):
            points = extrema.loc[extrema["kind"].eq(kind)]
            axes.scatter(points["route_order"], points["fitted_elevation_m"],
                         marker=marker, color=color, label=kind.capitalize() + "s", zorder=5)
    if "fitted_elevation_m" in profile or extrema is not None:
        axes.legend()
    axes.set(xlabel="Route order", ylabel="Elevation (m)", title="Elevation profile")
    axes.grid(alpha=0.3)
    figure.tight_layout()
    return figure

def cosine_family(x, coefficients, x_min: float, x_max: float):
    """Evaluate b0 + sum(bj * cos(j*pi*u)), with u = (x-x_min)/(x_max-x_min).

    coefficients[0] is the intercept; subsequent entries weight successive cosine harmonics.
    Returns a float for scalar x, otherwise an array with x's shape. Use the same domain bounds
    when fitting and evaluating. This evaluates a curve; it does not fit coefficients or enforce
    exact interpolation through observed elevations.
    """
    values, weights = _cosine_inputs(x, coefficients, x_min, x_max)
    u = (values - x_min) / (x_max - x_min)
    harmonics = np.arange(weights.size)
    result = np.cos(np.pi * u[..., np.newaxis] * harmonics) @ weights
    return float(result) if values.ndim == 0 else result


def _cosine_inputs(x, coefficients, x_min: float, x_max: float):
    values = np.asarray(x, dtype=float)
    weights = np.asarray(coefficients, dtype=float)
    if weights.ndim != 1 or weights.size == 0 or not np.isfinite(weights).all():
        raise ValueError("coefficients must be a non-empty, finite one-dimensional array.")
    if not np.isfinite(x_min) or not np.isfinite(x_max) or x_max <= x_min:
        raise ValueError("Domain bounds must be finite with x_max greater than x_min.")
    if not np.isfinite(values).all():
        raise ValueError("x must contain only finite values.")
    return values, weights


def cosine_family_derivative(x, coefficients, x_min: float, x_max: float):
    """Evaluate the analytical derivative of cosine_family with respect to x.

    Use the original coefficients and domain bounds. The intercept contributes zero.
    Returns a float for scalar x, otherwise an array with x's shape.
    With route_order as x, units are metres of elevation per route-order unit, not road gradient.
    """
    values, weights = _cosine_inputs(x, coefficients, x_min, x_max)
    u = (values - x_min) / (x_max - x_min)
    harmonics = np.arange(weights.size)
    slopes = -weights * harmonics * np.pi / (x_max - x_min)
    result = np.sin(np.pi * u[..., np.newaxis] * harmonics) @ slopes
    return float(result) if values.ndim == 0 else result


def count_peaks_and_valleys(coefficients, x_min: float, x_max: float) -> dict[str, int]:
    """Count interior extrema using the same classification as the plot."""
    extrema = find_peaks_and_valleys(coefficients, x_min, x_max)
    return {
        "peaks": int(extrema["kind"].eq("peak").sum()),
        "valleys": int(extrema["kind"].eq("valley").sum()),
    }


def find_peaks_and_valleys(coefficients, x_min: float, x_max: float) -> pd.DataFrame:
    """Return route_order, fitted_elevation_m and kind for interior peaks and valleys.

    cos(j*theta) = T_j(cos(theta)), so interior stationary points are roots of the
    derivative of the equivalent Chebyshev polynomial. A constant curve has no isolated extrema.
    """
    _, weights = _cosine_inputs([], coefficients, x_min, x_max)
    polynomial = np.polynomial.Chebyshev(weights)
    roots = polynomial.deriv().roots()
    real_roots = roots.real[np.isclose(roots.imag, 0, atol=1e-10, rtol=0)]
    real_roots = real_roots[(real_roots > -1) & (real_roots < 1)]
    positions = np.sort(x_min + np.arccos(real_roots) * (x_max - x_min) / np.pi)
    boundaries = np.concatenate(([x_min], positions, [x_max]))
    midpoints = boundaries[:-1] + np.diff(boundaries) / 2
    slopes = np.asarray(cosine_family_derivative(midpoints, weights, x_min, x_max))
    peaks = (slopes[:-1] > 0) & (slopes[1:] < 0)
    valleys = (slopes[:-1] < 0) & (slopes[1:] > 0)
    turning = peaks | valleys
    return pd.DataFrame({
        "route_order": positions[turning],
        "fitted_elevation_m": cosine_family(positions[turning], weights, x_min, x_max),
        "kind": np.where(peaks[turning], "peak", "valley"),
    })


def fit_cosine_profile(profile: pd.DataFrame, k: int) -> tuple[pd.DataFrame, np.ndarray]:
    """Fit the first k cosine harmonics plus an intercept by least squares.

    Returns a profile copy with fitted_elevation_m and the k+1 coefficients (intercept first).
    k=0 fits a constant. This is a regression approximation, not exact pointwise interpolation.
    To evaluate elsewhere, pass the coefficients and this profile's route_order bounds to cosine_family.
    """
    if isinstance(k, (bool, np.bool_)) or not isinstance(k, (int, np.integer)) or k < 0:
        raise ValueError("k must be a non-negative integer.")
    x = profile["route_order"].to_numpy(dtype=float)
    elevation = profile["elevation_m"].to_numpy(dtype=float)
    if len(x) < 2 or not np.isfinite(x).all() or not np.isfinite(elevation).all():
        raise ValueError("The profile must contain at least two finite route orders and elevations.")
    if k + 1 > np.unique(x).size:
        raise ValueError("The number of coefficients (k+1) cannot exceed the number of distinct route orders.")
    x_min, x_max = float(x.min()), float(x.max())
    if x_max <= x_min:
        raise ValueError("The profile must span more than one route order.")
    u = (x - x_min) / (x_max - x_min)
    design = np.cos(np.pi * u[:, np.newaxis] * np.arange(k + 1))
    coefficients, _, rank, _ = np.linalg.lstsq(design, elevation, rcond=None)
    if rank < k + 1:
        raise ValueError("The cosine fit is rank deficient. Use fewer cosine terms.")
    fitted = profile.copy()
    fitted["fitted_elevation_m"] = cosine_family(x, coefficients, x_min, x_max)
    fitted.attrs["cosine_coefficients"] = coefficients
    fitted.attrs["k"] = int(k)
    return fitted, coefficients


def log_likelihood(fitted: pd.DataFrame) -> float:
    """Gaussian log-likelihood assuming independent residuals with mean zero.

    Estimate variance by maximum likelihood: sigma_squared = SSE/n (not SSE/(n-k)).
    A perfect fit returns +inf because the likelihood is unbounded as variance approaches zero.
    """
    residuals = (fitted["elevation_m"] - fitted["fitted_elevation_m"]).to_numpy(dtype=float)
    if residuals.size == 0 or not np.isfinite(residuals).all():
        raise ValueError("Log-likelihood requires non-empty, finite elevation residuals.")
    variance = float(np.mean(np.square(residuals)))
    if variance == 0:
        return float("inf")
    return float(-residuals.size / 2 * (np.log(2 * np.pi) + np.log(variance) + 1))


def select_cosine_bic(
    profile: pd.DataFrame,
    max_k: int | None = None,
    *,
    on_progress: Callable[[int], None] | None = None,
) -> pd.DataFrame:
    """Search from k=1 with steps of 20, then refine using steps of 5 and 1.

    At each stage, stop at the first non-improvement and back up one step before refining.
    BIC counts k harmonics, an intercept and the estimated variance. The ceiling is floor(n/100),
    with n profile points; max_k can lower it further. on_progress receives k before each fit.
    This search does not guarantee a global minimum.
    """
    limit = min(profile["route_order"].nunique() - 1, len(profile) // 100)
    if max_k is not None:
        if isinstance(max_k, (bool, np.bool_)) or not isinstance(max_k, (int, np.integer)) or max_k < 1:
            raise ValueError("max_k must be a positive integer.")
        limit = min(limit, max_k)
    if limit < 1:
        raise ValueError("BIC selection requires at least 100 points and two distinct route orders.")

    def evaluate(k: int) -> tuple[pd.DataFrame, float]:
        if on_progress is not None:
            on_progress(k)
        fitted, _ = fit_cosine_profile(profile, k)
        score = np.log(len(profile)) * (k + 2) - 2 * log_likelihood(fitted)
        print(f"k={k}: BIC={score:,.2f}")
        return fitted, score

    best_k = 1
    best_fit, best_bic = evaluate(1)
    lower = 1
    for step in (20, 5, 1):
        current_k = lower
        current_fit, current_bic = evaluate(current_k)
        previous_k = current_k
        while current_k < limit and not np.isneginf(best_bic):
            trial_k = min(current_k + step, limit)
            trial_fit, trial_bic = evaluate(trial_k)
            if trial_bic < best_bic:
                best_k, best_fit, best_bic = trial_k, trial_fit, trial_bic
            if trial_bic < current_bic:
                previous_k, current_k = current_k, trial_k
                current_bic = trial_bic
            else:
                limit = trial_k - 1
                break
        lower = previous_k
    print(f"Selected k={best_k}, BIC={best_bic:,.2f}")
    return best_fit


def add_strava_categories(segments: pd.DataFrame) -> pd.DataFrame:
    """Estimate Strava climb categories; apply the same scale to descents as an equivalent.

    Score = horizontal distance (metres) * abs(average grade in percent) = 100 * abs(net rise).
    Use original net elevation change, not total accumulated gain or maximum gradient.
    Thresholds are strict, as documented at:
    https://support.strava.com/en-us/articles/15402015-how-does-strava-categorize-climbs
    """
    result = segments.copy()
    distance_m = segments["distance_km"].to_numpy(dtype=float) * 1000
    gain = segments["elevation_gain_m"].to_numpy(dtype=float)
    loss = segments["elevation_loss_m"].to_numpy(dtype=float)
    if not all(np.isfinite(values).all() for values in (distance_m, gain, loss)):
        raise ValueError("Strava categorization requires finite distances and elevation gain/loss.")
    if (distance_m < 0).any() or (gain < 0).any() or (loss < 0).any():
        raise ValueError("Segment distance and elevation gain/loss must not be negative.")
    rise = gain - loss
    if ((distance_m == 0) & (rise != 0)).any():
        raise ValueError("Cannot categorize a zero-distance segment with a non-zero elevation change.")
    grade = np.divide(100 * rise, distance_m, out=np.zeros_like(rise), where=distance_m > 0)
    score = 100 * np.abs(rise)
    result["direction"] = np.where(rise > 0, "Uphill", np.where(rise < 0, "Downhill", "Level"))
    result["average_gradient_pct"] = grade
    result["strava_score"] = score
    result["strava_equivalent_category"] = np.select(
        [score > 80000, score > 64000, score > 32000, score > 16000, score > 8000],
        ["HC", "Cat 1", "Cat 2", "Cat 3", "Cat 4"],
        default="Uncategorized",
    )
    return result


def elevation_segments(profile: pd.DataFrame, extrema: pd.DataFrame) -> pd.DataFrame:
    """Split at fitted extrema and route endpoints, measuring original GPX edges.

    Fractional boundaries split distance and gain/loss proportionally along the original edge.
    Average signed original edge gradients in a centred five-sample window over the full route,
    using available samples at route endpoints. Report the largest absolute average overlapping each segment.
    Zero-distance edges with an elevation change have undefined gradient and are rejected.
    """
    orders = profile["route_order"].to_numpy(dtype=float)
    elevations = profile["elevation_m"].to_numpy(dtype=float)
    distances = profile["distance_cum_km"].to_numpy(dtype=float)
    if len(orders) < 2 or not all(np.isfinite(values).all() for values in (orders, elevations, distances)):
        raise ValueError("Segments require at least two finite route points, elevations and distances.")
    if (np.diff(orders) <= 0).any() or (np.diff(distances) < 0).any():
        raise ValueError("Route orders must increase and cumulative distance must not decrease.")
    edge_distance_m = np.diff(distances) * 1000
    edge_elevation = np.diff(elevations)
    if ((edge_distance_m == 0) & (edge_elevation != 0)).any():
        raise ValueError("Cannot calculate gradient: consecutive points have zero distance but different elevations.")
    gradients = np.divide(100 * edge_elevation, edge_distance_m,
                          out=np.zeros_like(edge_elevation), where=edge_distance_m > 0)
    gradients = pd.Series(gradients).rolling(5, center=True, min_periods=1).mean().abs().to_numpy()
    turning = extrema.sort_values("route_order")
    positions = turning["route_order"].to_numpy(dtype=float)
    if not np.isfinite(positions).all() or (positions <= orders[0]).any() or (positions >= orders[-1]).any():
        raise ValueError("Segment extrema must lie strictly inside the route.")
    boundaries = np.concatenate(([orders[0]], positions, [orders[-1]]))
    if (np.diff(boundaries) <= 0).any():
        raise ValueError("Segment boundaries must be distinct.")
    kinds = ["start", *turning["kind"].tolist(), "finish"]
    boundary_distances = np.interp(boundaries, orders, distances)
    rows = []
    for index, (start, end) in enumerate(zip(boundaries[:-1], boundaries[1:])):
        overlap = np.maximum(0, np.minimum(orders[1:], end) - np.maximum(orders[:-1], start))
        fraction = overlap / np.diff(orders)
        change = edge_elevation * fraction
        rows.append({
            "segment": index + 1,
            "from": kinds[index],
            "to": kinds[index + 1],
            "start_route_order": start,
            "end_route_order": end,
            "start_distance_km": boundary_distances[index],
            "end_distance_km": boundary_distances[index + 1],
            "distance_km": boundary_distances[index + 1] - boundary_distances[index],
            "elevation_gain_m": np.maximum(change, 0).sum(),
            "elevation_loss_m": -np.minimum(change, 0).sum(),
            "max_abs_gradient_pct": gradients[overlap > 0].max(),
        })
    return add_strava_categories(pd.DataFrame(rows))



if __name__ == "__main__":
    race_id = 'a03ff1d1-4546-4ab3-8c6b-63073816b190'
    profile = load_elevation_profile(race_id)
    print(profile.head(20))
    print(f"Loaded {len(profile):,} elevation points.")
    #fitted = select_cosine_bic(profile)
    fitted, coefficients = fit_cosine_profile(profile, 240)
    print(f"Gaussian log-likelihood: {log_likelihood(fitted):,.2f}")
    extrema = find_peaks_and_valleys(
        coefficients, float(profile["route_order"].min()), float(profile["route_order"].max()))
    peaks = int(extrema["kind"].eq("peak").sum())
    valleys = int(extrema["kind"].eq("valley").sum())
    print(f"Interior derivative sign changes: {peaks} peaks, {valleys} valleys ({peaks + valleys} total).")
    plot_elevation_profile(fitted, extrema)
    plt.show()