"""Run-rate forecast: stats helpers + SpendForecastDetector."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from clont.core.models import Money, Period
from clont.events.detectors import SpendForecastDetector
from clont.events.models import EventSeverity
from clont.events.stats import ewma, project_month_end
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


def test_project_month_end_run_rate():
    # 10 days at $10 -> MTD 100, rate 10, 21 remaining days -> 100 + 210 = 310.
    daily = [Decimal(10)] * 10
    assert project_month_end(daily, 31, 0.5) == Decimal(310)


def test_project_month_end_full_month_is_just_mtd():
    daily = [Decimal(10)] * 31
    assert project_month_end(daily, 31, 0.5) == Decimal(310)


def test_project_month_end_clamps_overrun():
    # More samples than days in month -> no negative remaining.
    daily = [Decimal(10)] * 33
    assert project_month_end(daily, 31, 0.5) == Decimal(330)


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
