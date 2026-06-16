"""Tag-hygiene report: cost-bearing resources missing required tags.

Untagged resources are the root cause of unattributable spend — you can't show
back, budget, or find an owner for a cost you can't label. This collector checks
every EC2 instance and EBS volume for the tag keys the operator declares
mandatory (``finops.required_tags``) and flags any that are missing one.

Governance, not direct savings, so the estimate is left unset. Config-gated: with
no ``required_tags`` configured the collector is a no-op.
"""

from __future__ import annotations

from decimal import Decimal

from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.base import FinOpsTuning
from clont.finops.models import CostRecord, Recommendation
from clont.providers.aws.parsing import _EBSVolume, _Instance
from clont.providers.aws.regions import for_each_region
from clont.providers.base import Provider

_KIND = "missing-tags"


@register("finops", Cloud.AWS, "tags")
class TagHygieneCollector:
    cloud = Cloud.AWS
    service = "tags"

    def __init__(self, provider: Provider, tuning: FinOpsTuning | None = None) -> None:
        self._provider = provider
        self._required = tuple(tuning.required_tags) if tuning else ()

    def collect(self, period: Period) -> list[CostRecord]:
        return []

    def recommendations(self, period: Period) -> list[Recommendation]:
        if not self._required:  # nothing mandated -> nothing to check
            return []
        return for_each_region(self._provider, self._region, what="tag hygiene")

    def _region(self, region: str) -> list[Recommendation]:
        ec2 = self._provider.client("ec2", region)
        out: list[Recommendation] = []
        out.extend(self._instances(ec2, region))
        out.extend(self._volumes(ec2, region))
        return out

    def _instances(self, ec2, region: str) -> list[Recommendation]:
        out: list[Recommendation] = []
        for page in ec2.get_paginator("describe_instances").paginate():
            for reservation in page.get("Reservations", []):
                for raw in reservation.get("Instances", []):
                    inst = _Instance.model_validate(raw)
                    if inst.state == "terminated":  # going away — don't nag
                        continue
                    missing = self._missing(inst.tag_map())
                    if missing:
                        out.append(self._rec("ec2", inst.instance_id, region, missing))
        return out

    def _volumes(self, ec2, region: str) -> list[Recommendation]:
        out: list[Recommendation] = []
        for page in ec2.get_paginator("describe_volumes").paginate():
            for raw in page.get("Volumes", []):
                vol = _EBSVolume.model_validate(raw)
                missing = self._missing(vol.tag_map())
                if missing:
                    out.append(self._rec("ebs", vol.volume_id, region, missing))
        return out

    def _missing(self, tags: dict[str, str]) -> list[str]:
        """Required keys absent or blank on this resource, in declared order."""
        return [k for k in self._required if not tags.get(k)]

    def _rec(self, service: str, rid: str, region: str, missing: list[str]) -> Recommendation:
        return Recommendation(
            cloud=str(Cloud.AWS),
            service=service,
            kind=_KIND,
            resource=CloudResource(
                cloud=Cloud.AWS,
                service=service,
                resource_id=rid,
                region=region,
                alias=self._provider.alias,
            ),
            summary=f"Missing required tag(s): {', '.join(missing)} — add for cost attribution",
            estimated_savings=Money(amount=Decimal(0)),
        )
