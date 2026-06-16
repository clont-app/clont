"""Load balancer monitoring: ELBv2 target health (unhealthy targets)."""

from __future__ import annotations

from clont.core.models import Cloud, CloudResource
from clont.core.registry import register
from clont.monitoring.aws._common import for_each_region
from clont.monitoring.models import HealthCheck, HealthStatus
from clont.providers.aws.parsing import _TargetHealth
from clont.providers.base import Provider


@register("monitoring", Cloud.AWS, "elbv2")
class ELBHealthCollector:
    cloud = Cloud.AWS
    service = "elbv2"

    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    def health(self) -> list[HealthCheck]:
        return for_each_region(self._provider, self._region_health, what="elb health")

    def _region_health(self, region: str) -> list[HealthCheck]:
        elb = self._provider.client("elbv2", region)
        checks: list[HealthCheck] = []
        for page in elb.get_paginator("describe_target_groups").paginate():
            for tg in page.get("TargetGroups", []):
                name = tg.get("TargetGroupName", tg.get("TargetGroupArn", "unknown"))
                resp = elb.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])
                states = [
                    _TargetHealth.model_validate(d).state
                    for d in resp.get("TargetHealthDescriptions", [])
                ]
                status, summary = self._evaluate(states)
                checks.append(
                    HealthCheck(
                        resource=CloudResource(
                            cloud=Cloud.AWS,
                            service="elbv2",
                            resource_id=name,
                            region=region,
                            alias=self._provider.alias,
                        ),
                        status=status,
                        summary=summary,
                    )
                )
        return checks

    @staticmethod
    def _evaluate(states: list[str]) -> tuple[HealthStatus, str]:
        # Only "unhealthy" counts against us. "initial" (registering) and
        # "draining" (deregistering) are transient, and "unused" means the
        # target group isn't attached to a listener — none are faults, so a
        # group that is e.g. all-"initial" mid-deploy reads OK rather than
        # alarming on every rollout.
        total = len(states)
        if total == 0:
            return HealthStatus.UNKNOWN, "no registered targets"
        unhealthy = sum(1 for s in states if s == "unhealthy")
        summary = f"{unhealthy}/{total} targets unhealthy"
        if unhealthy == total:
            return HealthStatus.CRITICAL, summary
        if unhealthy > 0:
            return HealthStatus.WARN, summary
        return HealthStatus.OK, summary
