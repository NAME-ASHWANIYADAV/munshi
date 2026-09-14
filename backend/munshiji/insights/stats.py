"""Robust statistics for the insight engines.

Pure functions only — no database, no network, no third-party dependencies. Everything here is
chosen to survive the shape of real shop data: small samples (8 weekday observations), heavy
tails (one wedding order triples a Tuesday) and occasional structural zeros (shop shut).

That is why the module leans on **median / MAD** rather than mean / standard deviation: a single
outlier moves the mean and inflates the SD enough to hide a genuine 15% collection dip, while the
median and MAD barely notice it (breakdown point 50% vs 0%).
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Sequence

__all__ = [
    "MAD_TO_SIGMA",
    "ROBUST_Z_CONST",
    "Z_CAP",
    "chi2_sf",
    "chi_square",
    "clamp",
    "ewma",
    "ewma_series",
    "iqr",
    "mad",
    "mad_is_degenerate",
    "median",
    "percentile",
    "quintile_rank",
    "quintiles",
    "robust_z",
    "safe_div",
    "share_of",
    "trend_slope",
]

#: Scaling that makes the MAD a consistent estimator of sigma for Gaussian data.
#: ``sigma_hat = MAD_TO_SIGMA * MAD``.
MAD_TO_SIGMA = 1.4826

#: ``0.6745 == 1 / 1.4826 == Phi^-1(0.75)``. Multiplying by it and dividing by the raw MAD is
#: therefore identical to dividing by ``sigma_hat`` — the spec's formula, spelled the usual way.
ROBUST_Z_CONST = 0.6745

#: Ceiling on the magnitude :func:`robust_z` will report. See its docstring.
Z_CAP = 10.0


def clamp(value: float, low: float, high: float) -> float:
    """Constrain ``value`` to ``[low, high]``."""
    return max(low, min(high, value))


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    """``numerator / denominator``, returning ``default`` when the denominator is ~0."""
    if denominator == 0 or abs(denominator) < 1e-12:
        return default
    return numerator / denominator


def median(values: Sequence[float]) -> float:
    """Median of ``values``; ``0.0`` for an empty sequence."""
    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        return 0.0
    mid = count // 2
    if count % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2.0


def mad(values: Sequence[float], *, center: float | None = None) -> float:
    """**Raw** median absolute deviation — ``median(|x - median(x)|)``, unscaled.

    Multiply by :data:`MAD_TO_SIGMA` for a sigma-equivalent; :func:`robust_z` does the equivalent
    scaling itself via :data:`ROBUST_Z_CONST`.
    """
    if not values:
        return 0.0
    middle = median(values) if center is None else center
    return median([abs(float(value) - middle) for value in values])


def mad_is_degenerate(mad_value: float) -> bool:
    """True when the MAD collapsed to zero and :func:`robust_z` must fall back to an epsilon.

    Engines use this to *lower the reported confidence*: a zero MAD means more than half the
    baseline samples are identical, so the dispersion estimate carries no information.
    """
    return mad_value <= 0.0


def robust_z(
    x: float,
    med: float,
    mad_value: float,
    *,
    floor_ratio: float = 0.05,
    absolute_floor: float = 1.0,
) -> float:
    """``0.6745 * (x - med) / MAD`` — how many robust sigmas ``x`` sits from the median.

    **Zero-MAD fallback.** A degenerate MAD (more than half the sample identical, common with
    8 observations) would divide by zero. We substitute a floor scaled to the level of the series,
    ``eps = max(floor_ratio * |med|, absolute_floor)`` — i.e. we *assume* a typical day varies by
    at least ``floor_ratio`` (5%) around its median rather than pretending variance is zero, which
    would manufacture infinite z-scores. Callers must pair this with :func:`mad_is_degenerate` and
    report a reduced confidence, because the denominator is then an assumption, not a measurement.

    The result is clamped to +/- :data:`Z_CAP`. Past about five robust sigmas the exact figure
    changes no decision, and an unclamped degenerate baseline (an all-zero weekday, say) would
    otherwise report millions of sigmas and put that number in a spoken sentence.
    """
    if mad_is_degenerate(mad_value):
        if med == 0 and x == 0:
            return 0.0
        scale = max(abs(med) * floor_ratio, absolute_floor)
    else:
        scale = mad_value
    return clamp(ROBUST_Z_CONST * (x - med) / scale, -Z_CAP, Z_CAP)


def ewma_series(values: Sequence[float], alpha: float = 0.3) -> list[float]:
    """Exponentially weighted moving average, ``s_t = a*x_t + (1-a)*s_(t-1)``, ``s_0 = x_0``.

    ``values`` must be oldest-first: the last element carries weight ``alpha``.
    """
    if not values:
        return []
    a = clamp(alpha, 0.0, 1.0)
    smoothed = [float(values[0])]
    for raw in values[1:]:
        smoothed.append(a * float(raw) + (1.0 - a) * smoothed[-1])
    return smoothed


def ewma(values: Sequence[float], alpha: float = 0.3) -> float:
    """Final EWMA level of an oldest-first series; ``0.0`` when empty."""
    smoothed = ewma_series(values, alpha)
    return smoothed[-1] if smoothed else 0.0


def percentile(values: Sequence[float], q: float) -> float:
    """Linearly interpolated percentile (``q`` in 0–100), matching the "inclusive" convention.

    Equivalent to ``statistics.quantiles(..., method="inclusive")`` and to numpy's default.
    """
    ordered = sorted(float(value) for value in values)
    count = len(ordered)
    if count == 0:
        return 0.0
    if count == 1:
        return ordered[0]
    position = (count - 1) * clamp(q, 0.0, 100.0) / 100.0
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(position)]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def iqr(values: Sequence[float]) -> float:
    """Inter-quartile range, ``p75 - p25``. Zero for fewer than two values."""
    if len(values) < 2:
        return 0.0
    return percentile(values, 75.0) - percentile(values, 25.0)


def quintiles(values: Sequence[float]) -> list[float]:
    """The four cut points (p20, p40, p60, p80) that split ``values`` into quintiles."""
    if not values:
        return [0.0, 0.0, 0.0, 0.0]
    return [percentile(values, q) for q in (20.0, 40.0, 60.0, 80.0)]


def quintile_rank(value: float, cuts: Sequence[float]) -> int:
    """Quintile 1–5 for ``value`` given :func:`quintiles` cut points (1 = lowest).

    A degenerate distribution (every cut identical, e.g. every customer bought exactly once)
    cannot be split, so values on the cut are reported as the middle quintile rather than being
    swept into either extreme.
    """
    if not cuts:
        return 3
    if cuts[0] == cuts[-1]:
        if value < cuts[0]:
            return 1
        if value > cuts[0]:
            return 5
        return 3
    return min(5, bisect_right(list(cuts), value) + 1)


def trend_slope(values: Sequence[float]) -> float:
    """Least-squares slope of ``values`` against their index — change *per step*.

    Returns ``0.0`` for fewer than two points or a constant index (never happens here).
    """
    count = len(values)
    if count < 2:
        return 0.0
    mean_x = (count - 1) / 2.0
    mean_y = sum(float(value) for value in values) / count
    numerator = 0.0
    denominator = 0.0
    for index, raw in enumerate(values):
        dx = index - mean_x
        numerator += dx * (float(raw) - mean_y)
        denominator += dx * dx
    return safe_div(numerator, denominator, 0.0)


def share_of(part: float, whole: float) -> float:
    """``part / whole`` as a 0–1 share, guarding a zero denominator."""
    return safe_div(float(part), float(whole), 0.0)


def chi_square(observed: Sequence[float], expected: Sequence[float]) -> float:
    """Pearson statistic ``sum((O - E)^2 / E)``.

    Buckets with a non-positive expected count are skipped: they contribute an undefined term,
    and in payment-mix terms they mean "this method did not exist in the baseline", which the
    engine reports separately as a new method rather than as chi-square mass.
    """
    total = 0.0
    for obs, exp in zip(observed, expected, strict=True):
        if exp <= 0:
            continue
        delta = float(obs) - float(exp)
        total += delta * delta / float(exp)
    return total


def _gamma_series(a: float, x: float, *, iterations: int = 400, eps: float = 3e-12) -> float:
    """Lower regularised incomplete gamma ``P(a, x)`` by series expansion (for ``x < a + 1``)."""
    if x <= 0:
        return 0.0
    ap = a
    term = 1.0 / a
    total = term
    for _ in range(iterations):
        ap += 1.0
        term *= x / ap
        total += term
        if abs(term) < abs(total) * eps:
            break
    return total * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gamma_cf(a: float, x: float, *, iterations: int = 400, eps: float = 3e-12) -> float:
    """Upper regularised incomplete gamma ``Q(a, x)`` by continued fraction (for x >= a+1)."""
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, iterations + 1):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h * math.exp(-x + a * math.log(x) - math.lgamma(a))


def chi2_sf(x: float, df: int) -> float:
    """Upper-tail probability ``P(X^2 > x)`` for ``df`` degrees of freedom.

    Implemented from the regularised incomplete gamma function so the payment-mix engine can
    report a real p-value without pulling in scipy. Accurate to ~1e-10 across the range we use.
    """
    if df <= 0 or x <= 0:
        return 1.0
    a = df / 2.0
    y = x / 2.0
    if y < a + 1.0:
        return 1.0 - _gamma_series(a, y)
    return _gamma_cf(a, y)
