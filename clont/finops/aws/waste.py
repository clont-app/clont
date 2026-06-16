"""FinOps recommendations for deterministic waste — no analysis, just facts.

Each check is a single read + a known-cost rule, so the savings figure is a
concrete (approximate) dollar amount:

* unattached EBS volumes (``status == available``) — pay for nothing,
* unassociated Elastic IPs — billed while idle,
* gp2 volumes that could be gp3 — same/better performance, cheaper.

Regional; `collect()` is a no-op (recommendations only).
"""

from __future__ import annotations

from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.aws import pricing
from clont.finops.models import CostRecord, Recommendation
from clont.providers.aws.parsing import _EBSVolume, _EIP
from clont.providers.aws.regions import for_each_region
from clont.providers.base import Provider

_USD = "USD"


@register("finops", Cloud.AWS, "waste")
class WasteCollector:
    cloud = Cloud.AWS
    service = "waste"

    def __init__(self, provider: Provider, tuning=None) -> None:
        self._provider = provider

    def collect(self, period: Period) -> list[CostRecord]:
        return []

    def recommendations(self, period: Period) -> list[Recommendation]:
        return for_each_region(self._provider, self._region, what="waste")

    def _region(self, region: str) -> list[Recommendation]:
        ec2 = self._provider.client("ec2", region)
        out: list[Recommendation] = []
        out.extend(self._volumes(ec2, region))
        out.extend(self._addresses(ec2, region))
        return out

    def _volumes(self, ec2, region: str) -> list[Recommendation]:
        out: list[Recommendation] = []
        for page in ec2.get_paginator("describe_volumes").paginate():
            for raw in page.get("Volumes", []):
                vol = _EBSVolume.model_validate(raw)
                if vol.state == "available":  # unattached -> delete
                    out.append(self._rec(
                        "ebs", "unattached-ebs", vol.volume_id, region,
                        f"Unattached {vol.volume_type or 'volume'} ({vol.size} GiB) — delete",
                        pricing.ebs_monthly(vol.volume_type, vol.size),
                    ))
                elif vol.state == "in-use" and vol.volume_type == "gp2":  # migrate to gp3
                    out.append(self._rec(
                        "ebs", "gp2-gp3", vol.volume_id, region,
                        f"gp2 -> gp3 migration ({vol.size} GiB) — cheaper, same baseline performance",
                        pricing.ebs_gp2_to_gp3_monthly(vol.size),
                    ))
        return out

    def _addresses(self, ec2, region: str) -> list[Recommendation]:
        out: list[Recommendation] = []
        for raw in ec2.describe_addresses().get("Addresses", []):
            eip = _EIP.model_validate(raw)
            if eip.association_id:
                continue  # in use
            out.append(self._rec(
                "ec2", "unassociated-eip", eip.allocation_id or eip.public_ip, region,
                f"Unassociated Elastic IP {eip.public_ip} — release",
                pricing.EIP_MONTH,
            ))
        return out

    def _rec(self, service: str, kind: str, rid: str, region: str, summary: str, saving) -> Recommendation:
        return Recommendation(
            cloud=str(Cloud.AWS),
            service=service,
            kind=kind,
            resource=CloudResource(
                cloud=Cloud.AWS,
                service=service,
                resource_id=rid,
                region=region,
                alias=self._provider.alias,
            ),
            summary=summary,
            estimated_savings=Money(amount=saving, currency=_USD),
        )
