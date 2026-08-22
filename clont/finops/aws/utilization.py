"""Commitment efficiency: RI/SP utilization & coverage, from owned inventory.

Where `commitments.py` advises *buying*, this flags commitments you already hold
that are working poorly:

* **Utilization** — a Savings Plan or RI you're paying for but not fully using
  (committed spend you aren't consuming — direct waste).
* **Coverage** — eligible on-demand usage a commitment *would* discount but
  doesn't yet (savings left on the table).

Both used to be Cost Explorer ``Get*`` calls (four a cycle). They're now derived
from the same free inventory join `commitments.py` uses, so the figures are a
snapshot rather than Cost Explorer's 30-day window and won't match the console.

Account-level: the region sweep happens inside the inventory join.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.aws import inventory
from clont.finops.base import FinOpsTuning
from clont.finops.models import CostRecord, Recommendation

_USD = "USD"
_SNAPSHOT = "snapshot of current usage, not a 30-day average"


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@register("finops", Cloud.AWS, "utilization")
class CommitmentUtilizationCollector:
    cloud = Cloud.AWS
    service = "utilization"
    collect_every_seconds = 3600
    recommend_every_seconds = 3600

    def __init__(self, provider, tuning: FinOpsTuning | None = None) -> None:
        self._provider = provider
        self._tuning = tuning or FinOpsTuning()

    def collect(self, period: Period) -> list[CostRecord]:
        return []  # efficiency advice only, no spend records

    def recommendations(self, period: Period) -> list[Recommendation]:
        inv = inventory.build(self._provider)
        out: list[Recommendation] = []
        out.extend(self._sp_utilization(inv))
        out.extend(self._sp_coverage(inv))
        out.extend(self._ri_utilization(inv))
        out.extend(self._ri_coverage(inv))
        return out

    # --- savings plans

    def _sp_utilization(self, inv: inventory.Inventory) -> list[Recommendation]:
        if inv.committed_hourly <= 0:  # no Savings Plans held -> nothing to judge
            return []
        pct = inv.sp_utilization_pct
        if pct >= self._tuning.ri_sp_min_utilization:
            return []
        unused = _money(inv.sp_unused_monthly)
        return [self._rec(
            "savings-plans", "low-sp-utilization", "savings-plans",
            f"Savings Plans only {pct:.0f}% utilized — "
            f"{unused} {_USD}/month committed but unused ({_SNAPSHOT})",
            unused,
        )]

    def _sp_coverage(self, inv: inventory.Inventory) -> list[Recommendation]:
        if inv.uncovered_hourly <= 0:  # nothing eligible left to commit
            return []
        pct = inv.sp_coverage_pct
        if pct >= self._tuning.ri_sp_min_coverage:
            return []
        on_demand = _money(inv.uncovered_monthly)
        return [self._rec(
            "savings-plans", "low-sp-coverage", "savings-plans",
            f"Only {pct:.0f}% of eligible compute covered by Savings Plans — "
            f"{on_demand} {_USD}/month on-demand could be committed ({_SNAPSHOT})",
            Decimal(0),
        )]

    # --- reserved instances

    def _ri_utilization(self, inv: inventory.Inventory) -> list[Recommendation]:
        if inv.reserved_count <= 0:  # no RIs held -> nothing to judge
            return []
        pct = inv.ri_utilization_pct
        if pct >= self._tuning.ri_sp_min_utilization:
            return []
        return [self._rec(
            "reserved-instances", "low-ri-utilization", "reserved-instances",
            f"Reserved Instances only {pct:.0f}% utilized — "
            f"{inv.unused_reserved_hours} reserved hours unused "
            f"({inv.unused_reserved} instance(s) with no matching usage, {_SNAPSHOT})",
            Decimal(0),
        )]

    def _ri_coverage(self, inv: inventory.Inventory) -> list[Recommendation]:
        if not inv.uncovered:  # every running instance already has an RI
            return []
        pct = inv.ri_coverage_pct
        if pct >= self._tuning.ri_sp_min_coverage:
            return []
        return [self._rec(
            "reserved-instances", "low-ri-coverage", "reserved-instances",
            f"Only {pct:.0f}% of eligible usage covered by Reserved Instances — "
            f"{inv.uncovered_hours} on-demand hours could be reserved ({_SNAPSHOT})",
            Decimal(0),
        )]

    def _rec(self, service: str, kind: str, rid: str, summary: str, saving: Decimal) -> Recommendation:
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
