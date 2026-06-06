"""Generic CloudWatch metric collection (cloudwatch:GetMetricData)."""

from __future__ import annotations

from clont.core.models import Cloud, Period
from clont.core.registry import register
from clont.monitoring.models import HealthCheck, MetricPoint
from clont.providers.base import Provider


@register("monitoring", Cloud.AWS, "cloudwatch")
class CloudWatchCollector:
    cloud = Cloud.AWS
    service = "cloudwatch"

    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    def collect(self, period: Period) -> list[MetricPoint]:
        # TODO: cloudwatch.get_metric_data(...) -> MetricPoint list.
        raise NotImplementedError

    def health(self) -> list[HealthCheck]:
        return []
