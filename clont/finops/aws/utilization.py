"""Commitment efficiency from Cost Explorer: RI/SP utilization & coverage.

Where `commitments.py` advises *buying* Savings Plans / Reserved Instances, this
collector flags commitments you already hold that are working poorly:

* **Utilization** — a Savings Plan or RI you're paying for but not fully using
  (you've committed to spend you aren't consuming — direct waste).
* **Coverage** — eligible on-demand usage a commitment *would* discount but
  doesn't yet (savings left on the table).

Both are read-only Cost Explorer ``Get*`` calls on the global endpoint
(us-east-1), so this collector is account-level and does not iterate regions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from botocore.exceptions import ClientError

from clont.core.logging import get_logger
from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.base import FinOpsTuning
from clont.finops.models import CostRecord, Recommendation
from clont.providers.aws.parsing import (
    _RICoverage,
    _RIUtilization,
    _SPCoverage,
    _SPUtilization,
)
from clont.providers.base import Provider

log = get_logger("clont.finops.aws.utilization")

_LOOKBACK_DAYS = 30

# Expected "no commitments / not allowed" responses — logged once, not warned.
_NOT_AVAILABLE = {
    "DataUnavailableException",
    "AccessDeniedException",
    "AccessDenied",
}


@register("finops", Cloud.AWS, "utilization")
class CommitmentUtilizationCollector:
    cloud = Cloud.AWS
    service = "utilization"

    def __init__(self, provider: Provider, tuning: FinOpsTuning | None = None) -> None:
        self._provider = provider
        self._tuning = tuning or FinOpsTuning()
        self._warned: set[str] = set()

    def collect(self, period: Period) -> list[CostRecord]:
        return []  # efficiency advice only, no spend records

    def recommendations(self, period: Period) -> list[Recommendation]:
        ce = self._provider.client("ce", "us-east-1")
        end = datetime.now(UTC).date()  # Cost Explorer windows are UTC dates
        window = {
            "Start": (end - timedelta(days=_LOOKBACK_DAYS)).isoformat(),
            "End": end.isoformat(),
        }
        out: list[Recommendation] = []
        # Four independent queries; isolate each so one failure can't drop the rest.
        for label, fn in (
            ("savings plans utilization", self._sp_utilization),
            ("savings plans coverage", self._sp_coverage),
            ("reserved instance utilization", self._ri_utilization),
            ("reserved instance coverage", self._ri_coverage),
        ):
            try:
                out.extend(fn(ce, window))
            except Exception as exc:  # noqa: BLE001 - one query must not drop the others
                log.warning(
                    "%s failed for %s: %s", label, self._provider.alias, exc
                )
        return out

    # --- Savings Plans

    def _sp_utilization(self, ce, window: dict) -> list[Recommendation]:
        resp = self._call(
            ce.get_savings_plans_utilization, "savings plans utilization", TimePeriod=window
        )
        if resp is None:
            return []
        u = _SPUtilization.model_validate(resp.get("Total", {}))
        if u.total_commitment <= 0:  # no Savings Plans held -> nothing to judge
            return []
        if u.utilization_pct >= self._tuning.ri_sp_min_utilization:
            return []
        return [self._rec(
            "savings-plans", "low-sp-utilization", "savings-plans",
            f"Savings Plans only {u.utilization_pct:.0f}% utilized — "
            f"{u.unused_commitment} {u.currency} committed but unused",
            u.unused_commitment, u.currency,
        )]

    def _sp_coverage(self, ce, window: dict) -> list[Recommendation]:
        resp = self._call(
            ce.get_savings_plans_coverage, "savings plans coverage", TimePeriod=window
        )
        if resp is None:
            return []
        entries = resp.get("SavingsPlansCoverages", [])
        if not entries:
            return []
        c = _SPCoverage.model_validate(entries[-1])  # latest period
        if c.on_demand_cost <= 0 or c.coverage_pct >= self._tuning.ri_sp_min_coverage:
            return []
        return [self._rec(
            "savings-plans", "low-sp-coverage", "savings-plans",
            f"Only {c.coverage_pct:.0f}% of eligible compute covered by Savings Plans — "
            f"{c.on_demand_cost} USD on-demand could be committed",
            Decimal(0), "USD",
        )]

    # --- Reserved Instances

    def _ri_utilization(self, ce, window: dict) -> list[Recommendation]:
        resp = self._call(
            ce.get_reservation_utilization, "reserved instance utilization", TimePeriod=window
        )
        if resp is None:
            return []
        u = _RIUtilization.model_validate(resp.get("Total", {}))
        if u.purchased_hours <= 0:  # no RIs held -> nothing to judge
            return []
        if u.utilization_pct >= self._tuning.ri_sp_min_utilization:
            return []
        return [self._rec(
            "reserved-instances", "low-ri-utilization", "reserved-instances",
            f"Reserved Instances only {u.utilization_pct:.0f}% utilized — "
            f"{u.unused_hours} reserved hours unused",
            Decimal(0), "USD",
        )]

    def _ri_coverage(self, ce, window: dict) -> list[Recommendation]:
        resp = self._call(
            ce.get_reservation_coverage, "reserved instance coverage", TimePeriod=window
        )
        if resp is None:
            return []
        c = _RICoverage.model_validate(resp.get("Total", {}))
        if c.on_demand_hours <= 0 or c.coverage_pct >= self._tuning.ri_sp_min_coverage:
            return []
        return [self._rec(
            "reserved-instances", "low-ri-coverage", "reserved-instances",
            f"Only {c.coverage_pct:.0f}% of eligible usage covered by Reserved Instances — "
            f"{c.on_demand_hours} on-demand hours could be reserved",
            Decimal(0), "USD",
        )]

    def _call(self, fn, label: str, **kwargs) -> dict | None:
        """Invoke a Cost Explorer call, swallowing the expected "no data / not
        allowed" errors (logged once) and returning None then."""
        try:
            return fn(**kwargs)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _NOT_AVAILABLE:
                if label not in self._warned:
                    log.info(
                        "%s unavailable for %s: %s", label, self._provider.alias, exc
                    )
                    self._warned.add(label)
                return None
            raise

    def _rec(
        self, service: str, kind: str, rid: str, summary: str,
        saving: Decimal, currency: str,
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
            estimated_savings=Money(amount=saving, currency=currency),
        )
