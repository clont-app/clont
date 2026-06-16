"""Capacity forecasting: linregress / periods_to_cross + CapacityForecastDetector."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from clont.core.models import Cloud, CloudResource
from clont.events.detectors import CapacityForecastDetector
from clont.events.models import EventSeverity
from clont.events.stats import linregress, periods_to_cross
from clont.monitoring.models import MetricPoint


def test_linregress_exact_line():
    slope, intercept = linregress([0, 1, 2, 3], [0, 2, 4, 6])
    assert slope == pytest.approx(2.0)
    assert intercept == pytest.approx(0.0)


def test_linregress_needs_two_points():
    with pytest.raises(ValueError):
        linregress([1], [1])


def test_linregress_needs_spread():
    with pytest.raises(ValueError):
        linregress([5, 5, 5], [1, 2, 3])


def test_periods_to_cross_approaching():
    # declining 10->4 over x 0..3, slope -2 -> crosses 0 two units past last x.
    assert periods_to_cross([0, 1, 2, 3], [10, 8, 6, 4], 0) == pytest.approx(2.0)


def test_periods_to_cross_flat_is_none():
    assert periods_to_cross([0, 1, 2, 3], [5, 5, 5, 5], 0) is None


def test_periods_to_cross_moving_away_is_none():
    # rising away from the 0 floor -> never reaches it.
    assert periods_to_cross([0, 1, 2, 3], [4, 6, 8, 10], 0) is None


def test_periods_to_cross_already_past_is_none():
    # threshold 5 sits below the fitted line's start -> crossing is in the past.
    assert periods_to_cross([0, 1, 2, 3], [10, 20, 30, 40], 5) is None


def _series(metric: str, values: list[float], *, unit="Percent", rid="db-1") -> list[MetricPoint]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    resource = CloudResource(cloud=Cloud.AWS, service="rds", resource_id=rid, alias="prod")
    return [
        MetricPoint(
            name=metric,
            value=v,
            unit=unit,
            timestamp=start + timedelta(days=i),
            resource=resource,
        )
        for i, v in enumerate(values)
    ]


def test_free_storage_trending_to_empty_fires():
    # 60 -> 8 over 13 days (~-4/day); from 8 that's ~2 days to 0 -> within 14d.
    values = [60 - 4 * i for i in range(14)]
    [event] = CapacityForecastDetector(forecast_days=14).detect(
        _series("FreeStoragePercent", values)
    )
    assert event.key == "monitoring:forecast:prod:aws:rds:db-1:FreeStoragePercent"
    assert event.severity == EventSeverity.WARN
    assert "predicted full" in event.title


def test_disk_used_trending_to_full_fires():
    values = [50 + 3 * i for i in range(14)]  # rising toward 100
    [event] = CapacityForecastDetector(forecast_days=14).detect(
        _series("PercentageDiskSpaceUsed", values, rid="rs-1")
    )
    assert "PercentageDiskSpaceUsed" in event.key


def test_flat_series_is_silent():
    values = [50.0] * 14
    assert CapacityForecastDetector(forecast_days=14).detect(
        _series("FreeStoragePercent", values)
    ) == []


def test_improving_series_is_silent():
    values = [10 + 3 * i for i in range(14)]  # free storage growing -> no alarm
    assert CapacityForecastDetector(forecast_days=14).detect(
        _series("FreeStoragePercent", values)
    ) == []


def test_far_off_exhaustion_is_silent():
    # Declining very slowly: ~0.1/day from 60 -> ~600 days to empty, beyond horizon.
    values = [60 - 0.1 * i for i in range(14)]
    assert CapacityForecastDetector(forecast_days=14).detect(
        _series("FreeStoragePercent", values)
    ) == []


def test_too_few_points_is_silent():
    values = [60, 40, 20]  # 3 < min_points (4)
    assert CapacityForecastDetector(forecast_days=14).detect(
        _series("FreeStoragePercent", values)
    ) == []


def test_non_capacity_metric_ignored():
    values = [60 - 4 * i for i in range(14)]
    assert CapacityForecastDetector(forecast_days=14).detect(
        _series("CPUUtilization", values)
    ) == []
