"""EBS monitoring: volume status via ec2 describe_volume_status."""

from __future__ import annotations

from clont.core.models import Cloud, CloudResource
from clont.core.registry import register
from clont.monitoring.aws._common import MetricsPolicy, for_each_region
from clont.monitoring.models import HealthCheck, HealthStatus
from clont.providers.aws.parsing import _EBSVolumeStatus
from clont.providers.base import Provider

_HEALTH = {
    "ok": HealthStatus.OK,
    "impaired": HealthStatus.CRITICAL,
    "insufficient-data": HealthStatus.WARN,
}


@register("monitoring", Cloud.AWS, "ebs")
class EBSHealthCollector:
    cloud = Cloud.AWS
    service = "ebs"

    def __init__(self, provider: Provider, metrics: MetricsPolicy | None = None) -> None:
        self._provider = provider

    def health(self) -> list[HealthCheck]:
        return for_each_region(self._provider, self._region_health, what="ebs health")

    def _region_health(self, region: str) -> list[HealthCheck]:
        ec2 = self._provider.client("ec2", region)
        checks: list[HealthCheck] = []
        for page in ec2.get_paginator("describe_volume_status").paginate():
            for raw in page.get("VolumeStatuses", []):
                vol = _EBSVolumeStatus.model_validate(raw)
                checks.append(
                    HealthCheck(
                        resource=CloudResource(
                            cloud=Cloud.AWS,
                            service="ebs",
                            resource_id=vol.volume_id,
                            region=region,
                            alias=self._provider.alias,
                        ),
                        status=_HEALTH.get(vol.status, HealthStatus.UNKNOWN),
                        summary=f"status: {vol.status}",
                    )
                )
        return checks
