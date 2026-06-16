"""Default threshold rules: ThresholdRuleDetector (capacity/pressure)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from clont.core.models import Cloud, CloudResource
from clont.events.detectors import ThresholdRuleDetector
from clont.events.models import EventSeverity
from clont.monitoring.models import MetricPoint


def _point(metric: str, value: float, *, unit="Percent", service="rds", rid="db-1",
           when=None) -> MetricPoint:
    resource = CloudResource(cloud=Cloud.AWS, service=service, resource_id=rid, alias="prod")
    return MetricPoint(
        name=metric,
        value=value,
        unit=unit,
        timestamp=when or datetime(2024, 1, 1, tzinfo=UTC),
        resource=resource,
    )


def test_low_free_storage_fires():
    [event] = ThresholdRuleDetector().detect([_point("FreeStoragePercent", 8.0)])
    assert event.key == "monitoring:rule:prod:aws:rds:db-1:FreeStoragePercent"
    assert event.severity == EventSeverity.WARN
    assert "below" in event.message


def test_healthy_free_storage_silent():
    assert ThresholdRuleDetector().detect([_point("FreeStoragePercent", 55.0)]) == []


def test_high_disk_used_fires():
    [event] = ThresholdRuleDetector().detect(
        [_point("PercentageDiskSpaceUsed", 95.0, service="redshift", rid="rs-1")]
    )
    assert "PercentageDiskSpaceUsed" in event.key
    assert "above" in event.message


def test_low_cpu_credits_fires():
    [event] = ThresholdRuleDetector().detect(
        [_point("CPUCreditBalance", 5.0, unit="Count", service="ec2", rid="i-1")]
    )
    assert event.key.endswith("CPUCreditBalance")


def test_swap_pressure_fires():
    [event] = ThresholdRuleDetector().detect(
        [_point("SwapUsage", 120.0, unit="Megabytes", service="elasticache", rid="cc-1")]
    )
    assert "swap" in event.message.lower()


def test_swap_under_threshold_silent():
    assert ThresholdRuleDetector().detect(
        [_point("SwapUsage", 10.0, unit="Megabytes", service="elasticache", rid="cc-1")]
    ) == []


def test_only_latest_point_counts():
    # An earlier breach that has since recovered must not fire.
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    pts = [
        _point("FreeStoragePercent", 5.0, when=t0),                       # breached
        _point("FreeStoragePercent", 60.0, when=t0 + timedelta(hours=6)),  # recovered (latest)
    ]
    assert ThresholdRuleDetector().detect(pts) == []


def test_configurable_threshold():
    pts = [_point("FreeStoragePercent", 15.0)]
    assert ThresholdRuleDetector(free_storage_min_pct=10).detect(pts) == []   # 15 >= 10
    assert ThresholdRuleDetector(free_storage_min_pct=20).detect(pts)         # 15 < 20


def test_unknown_metric_ignored():
    assert ThresholdRuleDetector().detect([_point("CPUUtilization", 99.0)]) == []
