"""Run-rate forecast: stats helpers + SpendForecastDetector."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from clont.core.models import Money, Period
from clont.events.detectors import SpendForecastDetector
from clont.events.models import EventSeverity
from clont.events.stats import ewma, project_seasonal, seasonal_factors
from clont.finops.models import CostRecord


def test_ewma_constant_series_is_the_constant():
    assert ewma([10.0, 10.0, 10.0], 0.5) == 10.0


def test_ewma_recency_biased():
    # s0=1; s1=.5*2+.5*1=1.5; s2=.5*3+.5*1.5=2.25
    assert ewma([1.0, 2.0, 3.0], 0.5) == pytest.approx(2.25)


def test_ewma_preserves_decimal_type():
    out = ewma([Decimal(10), Decimal(10), Decimal(10)], 0.5)
    assert isinstance(out, Decimal)
    assert out == Decimal(10)


def test_ewma_empty_raises():
    with pytest.raises(ValueError):
        ewma([], 0.5)


def _flat(n: int, amount: str = "10") -> list[tuple[int, Decimal]]:
    """`n` days at the same spend, weekdays cycling from Monday."""
    return [(i % 7, Decimal(amount)) for i in range(n)]


def test_project_seasonal_run_rate():
    # 10 days at $10 -> MTD 100, level 10, 21 remaining days -> 310.
    proj = project_seasonal(_flat(10), [i % 7 for i in range(21)], 0.5)
    assert proj.total == Decimal(310)
    assert proj.level == Decimal(10)


def test_project_seasonal_no_remaining_days_is_just_mtd():
    proj = project_seasonal(_flat(31), [], 0.5)
    assert proj.total == Decimal(310)
    assert proj.band == 0             # nothing left to be uncertain about


def test_project_seasonal_flat_series_is_a_confident_point():
    # identical days -> zero spread -> a point, and a *trusted* one
    proj = project_seasonal(_flat(10), [0, 1, 2], 0.5)
    assert proj.band == 0
    assert proj.low == proj.high == proj.total


def test_project_seasonal_single_day_has_no_band_at_all():
    proj = project_seasonal([(0, Decimal(10))], [1, 2], 0.5)
    assert proj.band is None


def test_project_seasonal_weekday_shape_beats_flat_rate():
    # weekdays $10, weekend $2, two full weeks -> the remaining days' mix matters
    samples = [(d, Decimal(10) if d < 5 else Decimal(2)) for _ in range(2) for d in range(7)]
    weekend = project_seasonal(samples, [5, 6], 0.5)
    week = project_seasonal(samples, [0, 1], 0.5)
    assert weekend.shaped and week.shaped
    mtd = Decimal(2) * (5 * 10 + 2 * 2)
    assert weekend.total == mtd + Decimal(4)      # 2 + 2
    assert week.total == mtd + Decimal(20)        # 10 + 10


def test_project_seasonal_band_narrows_as_the_month_fills():
    # same noisy daily pattern, day 4 vs day 20 -> the later forecast is tighter
    pattern = [Decimal(10), Decimal(30), Decimal(5), Decimal(25)] * 8
    early = project_seasonal(
        [(i % 7, pattern[i]) for i in range(4)], [i % 7 for i in range(27)], 0.5
    )
    late = project_seasonal(
        [(i % 7, pattern[i]) for i in range(20)], [i % 7 for i in range(11)], 0.5
    )
    assert early.band_pct > late.band_pct


def test_project_seasonal_low_never_undercuts_money_already_spent():
    proj = project_seasonal([(0, Decimal(100)), (1, Decimal(1))], [2, 3, 4], 0.5)
    assert proj.low >= Decimal(101)


def test_project_seasonal_estimates_days_the_window_missed():
    # window starts on the 10th: 9 unobserved days are priced at the level
    proj = project_seasonal(_flat(5), [0, 1], 0.5, missing=9)
    assert proj.total == Decimal(50) + Decimal(20) + Decimal(90)


def test_seasonal_factors_need_two_samples_per_phase():
    one_week = [(d, Decimal(10)) for d in range(7)]
    assert seasonal_factors([p for p, _ in one_week], [v for _, v in one_week]) == {}


def test_project_seasonal_empty_raises():
    with pytest.raises(ValueError):
        project_seasonal([], [1], 0.5)


def _cost(service: str, amount: str, day: date, alias: str = "prod") -> CostRecord:
    return CostRecord(
        cloud="aws",
        service=service,
        period=Period(start=day, end=day),
        cost=Money(amount=Decimal(amount)),
        alias=alias,
    )


def _month(n_days: int, daily: str, alias: str = "prod") -> list[CostRecord]:
    """`n_days` consecutive January days at `daily` dollars on EC2."""
    return [_cost("EC2", daily, date(2024, 1, 1) + timedelta(days=i), alias)
            for i in range(n_days)]


def test_forecast_one_info_event_per_account():
    records = _month(10, "10", "prod") + _month(10, "5", "staging")
    events = SpendForecastDetector(0.5).detect(records)
    by_key = {e.key: e for e in events}
    assert set(by_key) == {
        "finops:spend:forecast:prod",
        "finops:spend:forecast:staging",
    }
    assert all(e.severity == EventSeverity.INFO for e in events)


def test_forecast_projects_to_month_end():
    # 10 days at $10 in a 31-day month -> ~310 projected.
    [event] = SpendForecastDetector(0.5).detect(_month(10, "10"))
    assert event.payload["mtd"] == "100"
    assert Decimal(event.payload["forecast"]) == Decimal(310)
    assert event.payload["days_in_month"] == "31"
    assert event.title.startswith("[prod]")


def test_forecast_ignores_prior_month_days():
    # Late-Dec spillover + early Jan: only the latest month (Jan) is forecast.
    records = [
        _cost("EC2", "99", date(2023, 12, 30)),
        _cost("EC2", "99", date(2023, 12, 31)),
        _cost("EC2", "10", date(2024, 1, 1)),
        _cost("EC2", "10", date(2024, 1, 2)),
    ]
    [event] = SpendForecastDetector(0.5).detect(records)
    assert event.payload["mtd"] == "20"          # only the two January days
    assert event.payload["days_elapsed"] == "2"


def _noisy_month(n_days: int, alias: str = "prod") -> list[CostRecord]:
    """January days with a weekend dip — Jan 1 2024 is a Monday."""
    out = []
    for i in range(n_days):
        day = date(2024, 1, 1) + timedelta(days=i)
        out.append(_cost("EC2", "2" if day.weekday() >= 5 else "10", day, alias))
    return out


def _days(amounts: list[str], alias: str = "prod") -> list[CostRecord]:
    """One January day per amount, from Jan 1."""
    return [_cost("EC2", a, date(2024, 1, 1) + timedelta(days=i), alias)
            for i, a in enumerate(amounts)]


def test_forecast_says_it_is_too_early_on_a_short_noisy_window():
    [event] = SpendForecastDetector(0.5).detect(_days(["10", "30", "5", "25"]))
    assert event.payload["confidence"] == "low"
    assert Decimal(event.payload["forecast_low"]) < Decimal(event.payload["forecast_high"])
    assert "too early" in event.message


def test_forecast_trusts_a_predictable_month():
    [event] = SpendForecastDetector(0.5).detect(_noisy_month(24))
    assert event.payload["confidence"] == "normal"
    assert event.payload["basis"] == "weekday-shaped"


def test_forecast_prices_remaining_weekends_at_weekend_rates():
    # Jan 20 2024 is a Saturday: 11 days left, 3 of them weekend.
    [event] = SpendForecastDetector(0.5).detect(_noisy_month(20))
    mtd = Decimal(event.payload["mtd"])
    flat = mtd + mtd / 20 * 11                             # blind run rate
    assert Decimal(event.payload["forecast"]) == mtd + Decimal(8) * 10 + Decimal(3) * 2
    assert Decimal(event.payload["forecast"]) != flat      # the mix is not the average


def test_forecast_low_band_never_undercuts_mtd():
    [event] = SpendForecastDetector(0.5).detect(_days(["100", "1", "1"]))
    assert Decimal(event.payload["forecast_low"]) >= Decimal(event.payload["mtd"])
