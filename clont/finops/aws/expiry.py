"""Commitment expiry calendar: RIs and Savings Plans about to lapse.

`commitments.py` advises buying and `utilization.py` judges what you hold; this
watches the *end dates*. A lapsed commitment moves its usage back to on-demand
overnight, which shows up days later as an unexplained spend spike — the cause is
already knowable today, for free, from the same inventory join (the end date
rides along on `describe_reserved_instances` / `describe_savings_plans`).

Two decisions worth knowing:

* **The tier is part of the rec kind** (`commitment-expiry-60d` / `-30d` /
  `-7d`), so the warning escalates instead of being swallowed. Notification
  channels dedupe on the event key, which carries the kind — one key for the
  whole window would mean a single alert at 60 days and silence at 7.
* **The dollar figure is the discount at risk, not the bill.** Renewing doesn't
  make the usage free, it keeps the ~20-25% a commitment takes off it, so that
  is what the saving names. It's the coarse price table, so it's approximate and
  says so.

Account-level: the region sweep happens inside the inventory join.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.aws import inventory, pricing
from clont.finops.models import CostRecord, Recommendation
from clont.providers.base import Provider

_USD = "USD"
_TERMS = "one year, no upfront"
# ascending: the tightest threshold already crossed is the one reported
_TIERS = (7, 30, 60)
_SECONDS_PER_DAY = 86400


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _days_left(end: datetime, now: datetime) -> int:
    """Whole days until `end`; never negative, so a lapsed one still reports."""
    return max(0, int((end - now).total_seconds() // _SECONDS_PER_DAY))


def _tier(days: int) -> int | None:
    return next((t for t in _TIERS if days <= t), None)


def _when(days: int) -> str:
    if days == 0:
        return "expires today"
    return f"expires in {days} day{'s' if days > 1 else ''}"


@register("finops", Cloud.AWS, "expiry")
class CommitmentExpiryCollector:
    cloud = Cloud.AWS
    service = "expiry"
    # free describes, and inventory.build() has its own ttl underneath
    collect_every_seconds = 3600
    recommend_every_seconds = 3600

    def __init__(self, provider: Provider, tuning=None) -> None:
        self._provider = provider

    def collect(self, period: Period) -> list[CostRecord]:
        return []  # expiry advice only, no spend records

    def recommendations(self, period: Period) -> list[Recommendation]:
        inv = inventory.build(self._provider)
        now = datetime.now(UTC)
        out: list[Recommendation] = []
        for item in inv.reserved:
            rec = self._reserved(item, now)
            if rec is not None:
                out.append(rec)
        for plan in inv.all_plans:
            rec = self._plan(plan, now)
            if rec is not None:
                out.append(rec)
        return out

    def _reserved(self, item: inventory.ReservedItem, now: datetime) -> Recommendation | None:
        if item.end is None:  # no end date described -> nothing to warn about
            return None
        days = _days_left(item.end, now)
        tier = _tier(days)
        if tier is None:
            return None
        quote = pricing.instance_quote(item.instance_type, item.region)
        on_demand = quote.amount * item.count * pricing.HOURS_PER_MONTH
        at_risk = _money(on_demand * pricing.RI_DISCOUNT_PCT)
        scope = f"{item.region}/{item.az}" if item.az else item.region
        return Recommendation(
            cloud=str(Cloud.AWS),
            service="reserved-instances",
            kind=f"commitment-expiry-{tier}d",
            resource=CloudResource(
                cloud=Cloud.AWS,
                service="reserved-instances",
                resource_id=item.reservation_id,
                region=item.region,
                alias=self._provider.alias,
            ),
            summary=(
                f"Reserved Instance {item.count}x {item.instance_type} in {scope} "
                f"{_when(days)} ({item.end:%Y-%m-%d}) — that usage reverts to "
                f"on-demand; renew ({_TERMS}) or roll it into a Compute Savings Plan"
            ),
            estimated_savings=Money(amount=at_risk, currency=_USD),
            priced_region=quote.region,
            approximate=quote.approximate,
        )

    def _plan(self, plan, now: datetime) -> Recommendation | None:
        if plan.end is None:
            return None
        days = _days_left(plan.end, now)
        tier = _tier(days)
        if tier is None:
            return None
        commit = _money(plan.commitment)
        # committed $/hr is the *discounted* rate, so the uplift on lapsing is
        # d/(1-d) of it, not d
        d = pricing.SP_DISCOUNT_PCT
        at_risk = _money(plan.commitment * pricing.HOURS_PER_MONTH * d / (1 - d))
        family = f", {plan.ec2_instance_family}" if plan.ec2_instance_family else ""
        return Recommendation(
            cloud=str(Cloud.AWS),
            service="savings-plans",
            kind=f"commitment-expiry-{tier}d",
            resource=CloudResource(
                cloud=Cloud.AWS,
                service="savings-plans",
                resource_id=plan.plan_id,
                alias=self._provider.alias,
            ),
            summary=(
                f"{plan.plan_type} Savings Plan{family} at {commit} {plan.currency}/hr "
                f"{_when(days)} ({plan.end:%Y-%m-%d}) — covered usage reverts to "
                f"on-demand; buy a replacement committing {commit} {plan.currency}/hr "
                f"({_TERMS})"
            ),
            estimated_savings=Money(amount=at_risk, currency=_USD),
        )
