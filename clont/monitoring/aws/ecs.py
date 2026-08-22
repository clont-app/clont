"""ECS monitoring: service running vs desired count + deployment rollout state.

Per region: list clusters → list services → describe in batches of 10.
"""

from __future__ import annotations

from collections.abc import Iterator

from clont.core.models import Cloud, CloudResource
from clont.core.registry import register
from clont.monitoring.aws._common import MetricsPolicy, for_each_region
from clont.monitoring.models import HealthCheck, HealthStatus
from clont.providers.aws.parsing import _ECSService
from clont.providers.base import Provider

_DESCRIBE_BATCH = 10  # describe_services accepts at most 10 services per call.


def _chunks[T](items: list[T], size: int) -> Iterator[list[T]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


@register("monitoring", Cloud.AWS, "ecs")
class ECSHealthCollector:
    cloud = Cloud.AWS
    service = "ecs"

    def __init__(self, provider: Provider, metrics: MetricsPolicy | None = None) -> None:
        self._provider = provider

    def health(self) -> list[HealthCheck]:
        return for_each_region(self._provider, self._region_health, what="ecs health")

    def _region_health(self, region: str) -> list[HealthCheck]:
        ecs = self._provider.client("ecs", region)
        checks: list[HealthCheck] = []
        for cpage in ecs.get_paginator("list_clusters").paginate():
            for cluster in cpage.get("clusterArns", []):
                service_arns = self._service_arns(ecs, cluster)
                for batch in _chunks(service_arns, _DESCRIBE_BATCH):
                    resp = ecs.describe_services(cluster=cluster, services=batch)
                    for raw in resp.get("services", []):
                        checks.append(self._check(raw, region))
        return checks

    def _service_arns(self, ecs, cluster: str) -> list[str]:
        arns: list[str] = []
        for page in ecs.get_paginator("list_services").paginate(cluster=cluster):
            arns.extend(page.get("serviceArns", []))
        return arns

    def _check(self, raw: dict, region: str) -> HealthCheck:
        svc = _ECSService.model_validate(raw)
        failed = any(d.rollout_state == "FAILED" for d in svc.deployments)
        status, summary = self._evaluate(svc.running, svc.desired, failed)
        return HealthCheck(
            resource=CloudResource(
                cloud=Cloud.AWS,
                service="ecs",
                resource_id=svc.name,
                region=region,
                alias=self._provider.alias,
            ),
            status=status,
            summary=summary,
        )

    @staticmethod
    def _evaluate(running: int, desired: int, failed: bool) -> tuple[HealthStatus, str]:
        summary = f"{running}/{desired} running"
        if failed:
            return HealthStatus.CRITICAL, summary + "; deployment failed"
        if desired > 0 and running == 0:
            return HealthStatus.CRITICAL, summary
        if running < desired:
            return HealthStatus.WARN, summary
        return HealthStatus.OK, summary
