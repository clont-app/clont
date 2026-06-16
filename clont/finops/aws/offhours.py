"""Off-hours scheduling recommendations for always-on non-prod resources.

A dev/staging/test EC2 instance that runs 24/7 is paying for nights and weekends
nobody uses. We can't *act* (clont is read-only), but we can flag the candidates:
any running instance whose tags match the operator's non-prod convention
(``finops.nonprod_tags``) gets a "schedule me" recommendation.

Deterministic and config-gated: with no ``nonprod_tags`` configured the collector
is a no-op, so it never guesses which boxes are non-prod. The dollar value isn't
known (no per-instance price), so the saving is left unset and the recommendation
leads with the opportunity — stopping nights + weekends is roughly a 65–70% cut.
"""

from __future__ import annotations

from decimal import Decimal

from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.base import FinOpsTuning
from clont.finops.models import CostRecord, Recommendation
from clont.providers.aws.parsing import _Instance
from clont.providers.aws.regions import for_each_region
from clont.providers.base import Provider

_KIND = "off-hours-schedule"


@register("finops", Cloud.AWS, "offhours")
class OffHoursCollector:
    cloud = Cloud.AWS
    service = "offhours"

    def __init__(self, provider: Provider, tuning: FinOpsTuning | None = None) -> None:
        self._provider = provider
        self._tuning = tuning or FinOpsTuning()
        # Pre-lowercase the configured non-prod values for case-insensitive matching.
        self._nonprod = {
            key: {v.lower() for v in values}
            for key, values in self._tuning.nonprod_tags.items()
        }

    def collect(self, period: Period) -> list[CostRecord]:
        return []

    def recommendations(self, period: Period) -> list[Recommendation]:
        if not self._nonprod:  # no convention configured -> never guess
            return []
        return for_each_region(self._provider, self._region, what="off-hours ec2")

    def _region(self, region: str) -> list[Recommendation]:
        ec2 = self._provider.client("ec2", region)
        out: list[Recommendation] = []
        for page in ec2.get_paginator("describe_instances").paginate():
            for reservation in page.get("Reservations", []):
                for raw in reservation.get("Instances", []):
                    inst = _Instance.model_validate(raw)
                    if inst.state != "running":
                        continue
                    label = self._nonprod_label(inst.tag_map())
                    if label is None:
                        continue
                    out.append(self._rec(inst, region, label))
        return out

    def _nonprod_label(self, tags: dict[str, str]) -> str | None:
        """The matching `key=value` if any tag marks this instance non-prod."""
        for key, values in self._nonprod.items():
            value = tags.get(key)
            if value is not None and value.lower() in values:
                return f"{key}={value}"
        return None

    def _rec(self, inst: _Instance, region: str, label: str) -> Recommendation:
        return Recommendation(
            cloud=str(Cloud.AWS),
            service="ec2",
            kind=_KIND,
            resource=CloudResource(
                cloud=Cloud.AWS,
                service="ec2",
                resource_id=inst.instance_id,
                region=region,
                alias=self._provider.alias,
            ),
            summary=(
                f"Non-prod ({label}) instance running 24/7 — add a stop/start "
                "schedule for nights + weekends (~65% potential saving)"
            ),
            estimated_savings=Money(amount=Decimal(0)),
        )
