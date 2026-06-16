"""Tiny classic-statistics helpers for the anomaly detectors.

Plain arithmetic — no dependencies, no learned model, deterministic. Used to
build robust, seasonality-aware baselines (median + MAD) instead of a flat mean
that over-reacts to weekly / daily cycles.

Generic over `float` and `Decimal`: the operations (sort, subtract, divide,
abs) work on both, so the spend detector can keep its `Decimal` amounts while
the metric detector uses `float`.
"""

from __future__ import annotations

from decimal import Decimal

# Consistency constant: for normally distributed data, MAD * 1.4826 ≈ std, so
# (x - median) / (1.4826 * MAD) is on the same scale as a z-score. Equivalent to
# the common 0.6745 * (x - median) / MAD form.
_MAD_TO_STD = 1.4826


def mean(xs: list):
    """Arithmetic mean of a non-empty list (preserves float/Decimal type)."""
    if not xs:
        raise ValueError("mean() of empty list")
    return sum(xs) / len(xs)


def median(xs: list):
    """Median of a non-empty list (mean of the two middle items if even).

    Works for both `float` and `Decimal` lists; returns the input's numeric type.
    """
    if not xs:
        raise ValueError("median() of empty list")
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def mad(xs: list, center=None):
    """Median absolute deviation — a robust (outlier-resistant) spread.

    Returns the same numeric type as the input (float/Decimal). 0 means every
    sample equals the center (no spread).
    """
    med = center if center is not None else median(xs)
    return median([abs(x - med) for x in xs])


def modified_zscore(x, xs) -> float | None:
    """How many robust standard deviations `x` is from the cohort's median.

    Uses median + MAD so a few outliers in `xs` don't inflate the baseline.
    Returns `None` when the cohort has no spread (MAD == 0) — the caller should
    treat that as "can't judge" and skip, just like a zero standard deviation.
    """
    if not xs:
        return None
    med = median(xs)
    spread = mad(xs, med)
    if spread <= 0:
        return None
    return float(x - med) / (_MAD_TO_STD * float(spread))


def ewma(xs: list, alpha: float):
    """Exponentially-weighted moving average of a non-empty list.

    `s_t = alpha * x_t + (1 - alpha) * s_(t-1)`, so a larger `alpha` weights
    recent samples more. Used as a recency-biased daily spend rate for
    forecasting. Preserves the input's numeric type (float/Decimal): `alpha` is
    coerced to `Decimal` when the samples are `Decimal` to avoid type mixing.
    """
    if not xs:
        raise ValueError("ewma() of empty list")
    a = Decimal(str(alpha)) if isinstance(xs[0], Decimal) else alpha
    one_minus = (Decimal(1) - a) if isinstance(a, Decimal) else (1 - a)
    s = xs[0]
    for x in xs[1:]:
        s = a * x + one_minus * s
    return s


def project_month_end(daily_totals: list, days_in_month: int, alpha: float):
    """Forecast a full-month total from the days seen so far.

    `month-to-date + (EWMA daily rate) * remaining_days`, where
    `remaining_days = days_in_month - len(daily_totals)` (clamped at 0). Plain
    run-rate projection — deterministic, no learned model. Preserves the input's
    numeric type (float/Decimal).
    """
    if not daily_totals:
        raise ValueError("project_month_end() of empty list")
    rate = ewma(daily_totals, alpha)
    remaining = max(0, days_in_month - len(daily_totals))
    return sum(daily_totals) + rate * remaining


def linregress(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Ordinary least-squares fit of `ys` against `xs` -> ``(slope, intercept)``.

    Plain arithmetic, no dependency. Used to read the trend of a metric series
    (e.g. free storage declining over time) for capacity forecasting. Raises if
    there are fewer than two points or `xs` has no spread (a vertical fit is
    undefined).
    """
    n = len(xs)
    if n < 2 or len(ys) != n:
        raise ValueError("linregress() needs >=2 paired points")
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0:
        raise ValueError("linregress() needs spread in xs")
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    return slope, intercept


def periods_to_cross(xs: list[float], ys: list[float], threshold: float) -> float | None:
    """How far past the last `x` the fitted trend reaches `threshold`.

    Fits a least-squares line and extrapolates to where it crosses `threshold`,
    returning the lead time **in the same units as `xs`** measured from the last
    sample. Returns ``None`` when the series isn't genuinely heading toward the
    threshold — a flat trend, or one moving *away* (already past it, or receding) —
    so a forecast only fires for a real approach. The caller decides whether the
    lead time is alarmingly short.
    """
    slope, intercept = linregress(xs, ys)
    if slope == 0:
        return None
    last_x = xs[-1]
    last_y = slope * last_x + intercept            # fitted value, not raw
    cross_x = (threshold - intercept) / slope
    lead = cross_x - last_x
    if lead <= 0:
        return None                                # crossing is in the past
    # Must be moving toward the threshold, not away from it.
    if (threshold > last_y and slope <= 0) or (threshold < last_y and slope >= 0):
        return None
    return lead
