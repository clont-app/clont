"""Tiny classic-statistics helpers for the anomaly detectors.

Plain arithmetic — no dependencies, no learned model, deterministic. Used to
build robust, seasonality-aware baselines (median + MAD) instead of a flat mean
that over-reacts to weekly / daily cycles.

Generic over `float` and `Decimal`: the operations (sort, subtract, divide,
abs) work on both, so the spend detector can keep its `Decimal` amounts while
the metric detector uses `float`.
"""

from __future__ import annotations

from dataclasses import dataclass
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


def seasonal_factors(
    phases: list[int], values: list[Decimal], min_samples: int = 2
) -> dict[int, Decimal]:
    """Per-phase multiplier against the overall median, e.g. weekday -> 0.6.

    `phases[i]` labels the cycle position of `values[i]` (weekday, hour-of-day —
    the caller decides). A phase with fewer than `min_samples` observations, or a
    series with a non-positive median, gets no factor: returns `{}` so the caller
    falls back to a flat baseline instead of trusting one Sunday.
    """
    if len(phases) != len(values):
        raise ValueError("seasonal_factors() needs paired phases and values")
    center = median(values) if values else Decimal(0)
    if center <= 0:
        return {}
    groups: dict[int, list[Decimal]] = {}
    for phase, value in zip(phases, values):
        groups.setdefault(phase, []).append(value)
    factors = {
        phase: median(xs) / center
        for phase, xs in groups.items()
        if len(xs) >= min_samples
    }
    # a phase whose median is 0 would zero out that day; treat it as unknown
    return {p: f for p, f in factors.items() if f > 0}


@dataclass(frozen=True, slots=True)
class Projection:
    """A month-end forecast with the honesty attached.

    `low`/`high` bound the point estimate; `band` is the half-width they came
    from. `band = 0` is a *confident* point (a series with no spread, or a month
    with nothing left to predict); `band = None` means one single sample, which
    cannot disagree with itself — the only genuinely unknowable case. `shaped`
    says whether the weekday profile was used or the flat rate.
    """

    total: Decimal
    low: Decimal
    high: Decimal
    level: Decimal          # deseasonalized typical-day rate
    band: Decimal | None
    shaped: bool
    samples: int

    @property
    def band_pct(self) -> Decimal:
        if self.band is None or self.total <= 0:
            return Decimal(0)
        return self.band / self.total * 100


def project_seasonal(
    samples: list[tuple[int, Decimal]],
    remaining: list[int],
    alpha: float,
    missing: int = 0,
    min_samples: int = 2,
) -> Projection:
    """Month-end forecast that respects the weekly shape, with an error band.

    `samples` are `(phase, amount)` oldest-first for the days actually seen,
    `remaining` the phases of the days still to come, `missing` the count of
    early days the window never covered (estimated at the flat level, since
    their phases are unknown).

    The daily series is *deseasonalized* first — divided by its phase factor —
    so the EWMA measures the level rather than whichever weekdays happen to sit
    at the end of the window; each remaining day is then re-seasonalized. With
    too few samples per phase it degrades to the plain run rate.

    The band combines the two things that make an early forecast unreliable:
    uncertainty in the level (`sigma/sqrt(n)`, paid on every remaining day) and
    ordinary day-to-day noise (`sigma*sqrt(remaining)`). On day 2 the first term
    dominates and the range is wide; by day 25 both shrink. That is the answer to
    "same confidence on day 2 as on day 25".
    """
    if not samples:
        raise ValueError("project_seasonal() of empty samples")
    phases = [p for p, _ in samples]
    values = [v for _, v in samples]
    mtd = sum(values, Decimal(0))
    n = len(values)

    factors = seasonal_factors(phases, values, min_samples)
    deseason = [v / factors.get(p, Decimal(1)) for p, v in samples]
    level = ewma(deseason, alpha)

    ahead = sum((level * factors.get(p, Decimal(1)) for p in remaining), Decimal(0))
    total = mtd + ahead + level * missing

    days = len(remaining) + missing
    spread = Decimal(str(_MAD_TO_STD)) * mad(deseason)
    band: Decimal | None = None
    if days == 0 or n >= 2:
        level_err = spread / Decimal(n).sqrt() * days
        noise = spread * Decimal(days).sqrt()
        band = (level_err**2 + noise**2).sqrt()
    low = max(mtd, total - band) if band is not None else total
    high = total + band if band is not None else total
    return Projection(
        total=total,
        low=low,
        high=high,
        level=level,
        band=band,
        shaped=bool(factors),
        samples=n,
    )


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
