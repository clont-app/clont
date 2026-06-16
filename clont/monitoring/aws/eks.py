"""EKS monitoring: cluster control-plane health.

Two-step per region: `list_clusters` then `describe_cluster` per cluster. The
cluster's own `status` plus any `health.issues` AWS reports drive the verdict.
"""

from __future__ import annotations

from clont.core.models import Cloud, CloudResource
from clont.core.registry import register
from clont.monitoring.aws._common import for_each_region
from clont.monitoring.models import HealthCheck, HealthStatus
from clont.providers.aws.parsing import _EKSCluster
from clont.providers.base import Provider

# EKS cluster status -> health (when there are no explicit health issues).
# CREATING/UPDATING/DELETING/PENDING fall through to UNKNOWN.
_HEALTH = {
    "ACTIVE": HealthStatus.OK,
    "FAILED": HealthStatus.CRITICAL,
}


def _cluster_health(cluster: _EKSCluster) -> tuple[HealthStatus, str]:
    # AWS-reported issues mean something is wrong regardless of lifecycle status.
    if cluster.issues:
        codes = ", ".join(i.code for i in cluster.issues if i.code)
        return HealthStatus.CRITICAL, f"status: {cluster.status}; issues: {codes}"
    return _HEALTH.get(cluster.status, HealthStatus.UNKNOWN), f"status: {cluster.status}"


@register("monitoring", Cloud.AWS, "eks")
class EKSHealthCollector:
    cloud = Cloud.AWS
    service = "eks"

    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    def health(self) -> list[HealthCheck]:
        return for_each_region(self._provider, self._region_health, what="eks health")

    def _region_health(self, region: str) -> list[HealthCheck]:
        eks = self._provider.client("eks", region)
        checks: list[HealthCheck] = []
        for page in eks.get_paginator("list_clusters").paginate():
            for name in page.get("clusters", []):
                raw = eks.describe_cluster(name=name)["cluster"]
                cluster = _EKSCluster.model_validate(raw)
                status, summary = _cluster_health(cluster)
                checks.append(
                    HealthCheck(
                        resource=CloudResource(
                            cloud=Cloud.AWS,
                            service="eks",
                            resource_id=cluster.name,
                            region=region,
                            alias=self._provider.alias,
                        ),
                        status=status,
                        summary=summary,
                    )
                )
        return checks
