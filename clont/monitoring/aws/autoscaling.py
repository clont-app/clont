"""Auto Scaling monitoring: healthy in-service instances vs desired capacity."""

from __future__ import annotations

from clont.core.models import Cloud, CloudResource
from clont.core.registry import register
from clont.monitoring.aws._common import for_each_region
from clont.monitoring.models import HealthCheck, HealthStatus
from clont.providers.aws.parsing import _ASG
from clont.providers.base import Provider


@register("monitoring", Cloud.AWS, "autoscaling")
class AutoScalingHealthCollector:
    cloud = Cloud.AWS
    service = "autoscaling"

    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    def health(self) -> list[HealthCheck]:
        return for_each_region(self._provider, self._region_health, what="asg health")

    def _region_health(self, region: str) -> list[HealthCheck]:
        asg = self._provider.client("autoscaling", region)
        checks: list[HealthCheck] = []
        for page in asg.get_paginator("describe_auto_scaling_groups").paginate():
            for raw in page.get("AutoScalingGroups", []):
                group = _ASG.model_validate(raw)
                healthy = sum(
                    1
                    for i in group.instances
                    if i.health == "Healthy" and i.lifecycle == "InService"
                )
                status = self._status(healthy, group.desired)
                checks.append(
                    HealthCheck(
                        resource=CloudResource(
                            cloud=Cloud.AWS,
                            service="autoscaling",
                            resource_id=group.name,
                            region=region,
                            alias=self._provider.alias,
                        ),
                        status=status,
                        summary=f"{healthy}/{group.desired} healthy in-service",
                    )
                )
        return checks

    @staticmethod
    def _status(healthy: int, desired: int) -> HealthStatus:
        if desired == 0:
            return HealthStatus.OK  # nothing requested
        if healthy == 0:
            return HealthStatus.CRITICAL
        if healthy < desired:
            return HealthStatus.WARN
        return HealthStatus.OK
