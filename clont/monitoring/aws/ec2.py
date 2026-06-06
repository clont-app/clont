"""EC2 monitoring: instance metrics + reachability/health."""

from __future__ import annotations

from clont.core.models import Cloud, Period
from clont.core.registry import register
from clont.monitoring.models import HealthCheck, MetricPoint
from clont.providers.base import Provider


@register("monitoring", Cloud.AWS, "ec2")
class EC2MetricCollector:
    cloud = Cloud.AWS
    service = "ec2"

    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    def collect(self, period: Period) -> list[MetricPoint]:
        raise NotImplementedError

    def health(self) -> list[HealthCheck]:
        # TODO: describe_instance_status -> system/instance reachability.
        raise NotImplementedError
