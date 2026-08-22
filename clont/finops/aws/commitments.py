"""Commitment purchase advice (Savings Plans + RIs) from owned inventory.

Cost Explorer's ``Get*PurchaseRecommendation`` APIs are billed per request and
cost six calls a cycle. The headline advice they gave is derivable for free: take
the running instances no Reserved Instance already discounts, price them through
the coarse table, and apply a conservative commitment discount.

Two things that follow from *not* using Cost Explorer, both surfaced in the
summaries because an operator will otherwise compare against the console:

* it's a **snapshot** of what's running now, not a 30-day average, and
* only the share in `pricing.COMMIT_SAFETY` is advised, so a momentary spike
  can't turn into a year-long over-commitment.

Account-level: the region sweep happens inside the inventory join.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.aws import inventory, pricing
from clont.finops.models import CostRecord, Recommendation
from clont.providers.base import Provider

_USD = "USD"
_SP_TYPE = "COMPUTE_SP"
_TERMS = "one year, no upfront"


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@register("finops", Cloud.AWS, "commitments")
class CommitmentsCollector:
    cloud = Cloud.AWS
    service = "commitments"
    # free describes now, and inventory.build() has its own ttl underneath
    collect_every_seconds = 3600
    recommend_every_seconds = 3600

    def __init__(self, provider: Provider, tuning=None) -> None:
        self._provider = provider

    def collect(self, period: Period) -> list[CostRecord]:
        return []  # commitment advice only, no spend records

    def recommendations(self, period: Period) -> list[Recommendation]:
        inv = inventory.build(self._provider)
        uncovered = inv.uncovered_hourly
        if not inv.uncovered or uncovered <= 0:
            return []  # everything running is already under a commitment
        return self._savings_plan(inv, uncovered) + self._reserved_instances(inv, uncovered)

    def _savings_plan(self, inv: inventory.Inventory, uncovered: Decimal) -> list[Recommendation]:
        saving = _money(pricing.commitment_monthly_saving(uncovered, pricing.SP_DISCOUNT_PCT))
        if saving <= 0:
            return []
        commit = _money(pricing.commitment_hourly(uncovered))
        pct = pricing.SP_DISCOUNT_PCT * 100
        return [self._rec(
            "savings-plans", "savings-plan", _SP_TYPE,
            f"Compute Savings Plan ({_TERMS}): commit ${commit}/hr to cover "
            f"{len(inv.uncovered)} uncommitted instance(s) for ~{pct:.0f}% off "
            f"(snapshot of current usage, not a 30-day average)",
            saving,
        )]

    def _reserved_instances(
        self, inv: inventory.Inventory, uncovered: Decimal
    ) -> list[Recommendation]:
        saving = _money(pricing.commitment_monthly_saving(uncovered, pricing.RI_DISCOUNT_PCT))
        if saving <= 0:
            return []
        pct = pricing.RI_DISCOUNT_PCT * 100
        # Types are named so the advice is actionable; RIs are bought per type.
        types = sorted({r.instance_type for r in inv.uncovered if r.instance_type})
        listed = ", ".join(types[:3]) + (" and more" if len(types) > 3 else "")
        return [self._rec(
            "reserved-instances", "reserved-instance", "ec2",
            f"Reserved Instances for ec2 ({_TERMS}) — ~{pct:.0f}% off "
            f"{len(inv.uncovered)} uncommitted instance(s)"
            + (f": {listed}" if listed else "")
            + " (snapshot of current usage, not a 30-day average)",
            saving,
        )]

    def _rec(
        self, service: str, kind: str, rid: str, summary: str, saving: Decimal
    ) -> Recommendation:
        return Recommendation(
            cloud=str(Cloud.AWS),
            service=service,
            kind=kind,
            resource=CloudResource(
                cloud=Cloud.AWS,
                service=service,
                resource_id=rid,
                alias=self._provider.alias,
            ),
            summary=summary,
            estimated_savings=Money(amount=saving, currency=_USD),
        )
