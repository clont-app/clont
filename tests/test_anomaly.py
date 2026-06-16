"""Metric-anomaly detection: baseline + deviation over a metric series."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from clont.core.models import Cloud, CloudResource
from clont.events.detectors import MetricAnomalyDetector
from clont.events.models import EventSeverity
from clont.monitoring.models import MetricPoint

_T0 = datetime(2024, 1, 1, tzinfo=UTC)


def _series(values: list[float], *, name="CPUUtilization", unit="Percent",
            resource_id="i-1", alias="prod") -> list[MetricPoint]:
    resource = CloudResource(
        cloud=Cloud.AWS, service="ec2", resource_id=resource_id, alias=alias
    )
    return [
        MetricPoint(
            name=name, value=v, unit=unit,
            timestamp=_T0 + timedelta(hours=i), resource=resource,
        )
        for i, v in enumerate(values)
    ]


def test_spike_far_above_baseline_is_flagged():
    # Flat-ish baseline around 10, then a jump to 90.
    points = _series([10, 11, 9, 10, 12, 8, 10, 90])
    [event] = MetricAnomalyDetector(sigma=3.0, min_points=6).detect(points)
    assert event.key == "monitoring:anomaly:prod:aws:ec2:i-1:CPUUtilization"
    assert event.severity == EventSeverity.WARN
    assert "above" in event.message
    assert event.resource.resource_id == "i-1"


def test_drop_far_below_baseline_is_flagged():
    points = _series([100, 98, 102, 99, 101, 100, 97, 2])
    [event] = MetricAnomalyDetector(sigma=3.0, min_points=6).detect(points)
    assert "below" in event.message


def test_normal_variation_is_silent():
    points = _series([10, 11, 9, 10, 12, 8, 10, 11])
    assert MetricAnomalyDetector(sigma=3.0, min_points=6).detect(points) == []


def test_flat_baseline_is_skipped():
    # Perfectly flat baseline has zero spread -> no meaningful sigma.
    points = _series([5, 5, 5, 5, 5, 5, 5, 9])
    assert MetricAnomalyDetector(sigma=3.0, min_points=6).detect(points) == []


def test_too_few_points_is_skipped():
    points = _series([10, 11, 9, 90])  # only 3 baseline samples
    assert MetricAnomalyDetector(sigma=3.0, min_points=6).detect(points) == []


def test_metrics_and_resources_are_keyed_separately():
    cpu = _series([10, 11, 9, 10, 12, 8, 10, 90], name="CPUUtilization", resource_id="i-1")
    net = _series([1, 1, 1, 2, 1, 1, 1, 50], name="NetworkIn", unit="Bytes", resource_id="i-1")
    other = _series([10, 11, 9, 10, 12, 8, 10, 90], resource_id="i-2")
    events = MetricAnomalyDetector(sigma=3.0, min_points=6).detect(cpu + net + other)
    keys = {e.key for e in events}
    assert keys == {
        "monitoring:anomaly:prod:aws:ec2:i-1:CPUUtilization",
        "monitoring:anomaly:prod:aws:ec2:i-1:NetworkIn",
        "monitoring:anomaly:prod:aws:ec2:i-2:CPUUtilization",
    }


def _daily_cycle(latest_2pm: float, *, days: int = 4) -> list[MetricPoint]:
    """A daily cycle: 2pm busy (~80, rising), 2am/10pm quiet (~10), over `days`
    days, then a latest point the next day at 2pm with value `latest_2pm`."""
    resource = CloudResource(cloud=Cloud.AWS, service="ec2", resource_id="i-1", alias="prod")
    pts: list[MetricPoint] = []
    for d in range(days):
        base = _T0 + timedelta(days=d)
        for hour, val in [(2, 10.0), (14, 80.0 + d), (22, 11.0)]:
            pts.append(MetricPoint(
                name="CPUUtilization", value=val, unit="Percent",
                timestamp=base.replace(hour=hour), resource=resource,
            ))
    pts.append(MetricPoint(
        name="CPUUtilization", value=latest_2pm, unit="Percent",
        timestamp=(_T0 + timedelta(days=days)).replace(hour=14), resource=resource,
    ))
    return pts


def test_seasonal_normal_busy_hour_is_silent():
    # A normal 2pm sample (~81) matches prior 2pm values, so no anomaly — even
    # though it's far above the all-hours mean that the flat baseline would use.
    assert MetricAnomalyDetector(sigma=3.0, min_points=6).detect(_daily_cycle(81.0)) == []


def test_seasonal_same_hour_outlier_fires():
    [event] = MetricAnomalyDetector(sigma=3.0, min_points=6).detect(_daily_cycle(200.0))
    assert event.key == "monitoring:anomaly:prod:aws:ec2:i-1:CPUUtilization"
    assert "same-hour" in event.message
    assert "above" in event.message


def test_falls_back_to_flat_baseline_on_short_history():
    # One day, one sample per hour -> no same-hour cohort -> flat mean/std path.
    points = _series([10, 11, 9, 10, 12, 8, 10, 90])
    [event] = MetricAnomalyDetector(sigma=3.0, min_points=6).detect(points)
    assert "baseline" in event.message  # fallback basis label


def test_points_without_resource_are_ignored():
    points = [
        MetricPoint(name="CPUUtilization", value=v, unit="Percent",
                    timestamp=_T0 + timedelta(hours=i), resource=None)
        for i, v in enumerate([10, 11, 9, 10, 12, 8, 10, 90])
    ]
    assert MetricAnomalyDetector(sigma=3.0, min_points=6).detect(points) == []
