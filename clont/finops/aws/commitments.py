"""FinOps commitment recommendations from Cost Explorer (Savings Plans + RIs).

Cost Explorer analyses recent usage and proposes commitment purchases that would
cut on-demand spend. We surface the headline figures:

* **Savings Plans** — a Compute Savings Plan recommendation (the most flexible
  type), reported as one recommendation with its hourly commitment + monthly
  saving.
* **Reserved Instances** — per-service RI recommendations (EC2, RDS, ElastiCache,
  Redshift, OpenSearch), one per service that would save money.

Defaults to the lowest-commitment terms (one year, no upfront) so the figure is a
conservative floor; longer terms / upfront payment save more. Cost Explorer is a
global endpoint (us-east-1), so this collector is account-level and does not
iterate regions.
"""

from __future__ import annotations

from decimal import Decimal

from botocore.exceptions import ClientError

from clont.core.logging import get_logger
from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.models import CostRecord, Recommendation
from clont.providers.aws.parsing import _RIRecommendation, _SPSummary
from clont.providers.base import Provider

log = get_logger("clont.finops.aws.commitments")

# Conservative, flexible defaults: one-year, nothing upfront, 30-day lookback.
_TERM = "ONE_YEAR"
_PAYMENT = "NO_UPFRONT"
_LOOKBACK = "THIRTY_DAYS"
_SP_TYPE = "COMPUTE_SP"

# RI recommendations require a Service; cover the ones that commonly carry RIs.
# Maps Cost Explorer's service string -> the short id we tag the recommendation with.
_RI_SERVICES = {
    "Amazon Elastic Compute Cloud - Compute": "ec2",
    "Amazon Relational Database Service": "rds",
    "Amazon ElastiCache": "elasticache",
    "Amazon Redshift": "redshift",
    "Amazon OpenSearch Service": "opensearch",
}

# "We can't recommend anything" or "you're not allowed to ask" — expected, not a
# failure; logged once per cycle instead of warned.
_NOT_AVAILABLE = {
    "DataUnavailableException",
    "AccessDeniedException",
    "AccessDenied",
}


@register("finops", Cloud.AWS, "commitments")
class CommitmentsCollector:
    cloud = Cloud.AWS
    service = "commitments"

    def __init__(self, provider: Provider, tuning=None) -> None:
        self._provider = provider
        self._warned: set[str] = set()  # call labels already logged as unavailable

    def collect(self, period: Period) -> list[CostRecord]:
        return []  # commitment advice only, no spend records

    def recommendations(self, period: Period) -> list[Recommendation]:
        ce = self._provider.client("ce", "us-east-1")
        out: list[Recommendation] = []
        # Savings Plans and Reserved Instances are independent queries; isolate
        # them so an unexpected error on one still lets the other through.
        for label, fn in (("savings plans", self._savings_plans),
                          ("reserved instances", self._reserved_instances)):
            try:
                out.extend(fn(ce))
            except Exception as exc:  # noqa: BLE001 - one half must not drop the other
                log.warning(
                    "%s recommendations failed for %s: %s",
                    label, self._provider.alias, exc,
                )
        return out

    def _savings_plans(self, ce) -> list[Recommendation]:
        resp = self._call(
            ce.get_savings_plans_purchase_recommendation,
            "savings plans",
            SavingsPlansType=_SP_TYPE,
            TermInYears=_TERM,
            PaymentOption=_PAYMENT,
            LookbackPeriodInDays=_LOOKBACK,
        )
        if resp is None:
            return []
        rec = resp.get("SavingsPlansPurchaseRecommendation", {})
        summary = _SPSummary.model_validate(
            rec.get("SavingsPlansPurchaseRecommendationSummary", {})
        )
        if summary.estimated_monthly_savings <= 0:
            return []
        return [self._rec(
            "savings-plans", "savings-plan", _SP_TYPE,
            f"Compute Savings Plan ({_TERM.replace('_', ' ').lower()}, "
            f"{_PAYMENT.replace('_', ' ').lower()}): commit "
            f"${summary.hourly_commitment}/hr for ~{summary.savings_pct:.0f}% off",
            summary.estimated_monthly_savings, summary.currency,
        )]

    def _reserved_instances(self, ce) -> list[Recommendation]:
        out: list[Recommendation] = []
        for service_name, short in _RI_SERVICES.items():
            resp = self._call(
                ce.get_reservation_purchase_recommendation,
                f"reserved instances ({short})",
                Service=service_name,
                TermInYears=_TERM,
                PaymentOption=_PAYMENT,
                LookbackPeriodInDays=_LOOKBACK,
            )
            if resp is None:
                continue
            for raw in resp.get("Recommendations", []):
                ri = _RIRecommendation.model_validate(raw)
                if ri.estimated_monthly_savings <= 0:
                    continue
                out.append(self._rec(
                    "reserved-instances", "reserved-instance", short,
                    f"Reserved Instances for {short} "
                    f"({_TERM.replace('_', ' ').lower()}, "
                    f"{_PAYMENT.replace('_', ' ').lower()}) — ~{ri.savings_pct:.0f}% off",
                    ri.estimated_monthly_savings, ri.currency,
                ))
        return out

    def _call(self, fn, label: str, **kwargs) -> dict | None:
        """Invoke a Cost Explorer recommendation call, swallowing the expected
        "no data / not allowed" errors (logged once) and returning None then."""
        try:
            return fn(**kwargs)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _NOT_AVAILABLE:
                if label not in self._warned:
                    log.info(
                        "%s recommendations unavailable for %s: %s",
                        label, self._provider.alias, exc,
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
