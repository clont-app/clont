"""One join of what an account owns, shared by the commitment collectors.

This replaces Cost Explorer's recommendation APIs. Everything they answered —
what's covered, what's committed, what's idling — is derivable from three free
describes plus the coarse price table:

* ``ec2:DescribeInstances`` — running, non-spot instances, per region
* ``ec2:DescribeReservedInstances`` — active RIs, per region
* ``savingsplans:DescribeSavingsPlans`` — active Savings Plans, account-wide

**It's a snapshot, where Cost Explorer looked back 30 days.** Figures derived
from it will not match the console, and anything built on it should say so.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal

from clont.core.logging import get_logger
from clont.finops.aws import pricing
from clont.providers.aws.parsing import _Instance, _ReservedInstance, _SavingsPlan
from clont.providers.aws.regions import for_each_region
from clont.providers.base import Provider

log = get_logger("clont.finops.aws.inventory")

# The plan types that discount the EC2 usage we inventory.
_COMPUTE_PLANS = {"Compute", "EC2Instance"}

# Both commitment collectors run back-to-back in one cycle; share the sweep
# rather than describing every region twice.
_TTL = 60.0
_cache: dict[str, tuple[float, "Inventory"]] = {}


@dataclass(frozen=True)
class Running:
    """A running on-demand instance — a candidate for commitment coverage."""

    instance_id: str
    instance_type: str
    region: str
    az: str

    @property
    def hourly(self) -> Decimal:
        return pricing.instance_hourly(self.instance_type)


@dataclass
class ReservedPool:
    """Active RIs of one type in one scope, plus how many we matched to usage."""

    instance_type: str
    region: str
    az: str  # "" == regional scope, floats across the region
    count: int
    matched: int = 0

    @property
    def unused(self) -> int:
        return max(0, self.count - self.matched)


@dataclass(frozen=True)
class Inventory:
    running: list[Running]
    pools: list[ReservedPool]
    plans: list[_SavingsPlan]
    uncovered: list[Running] = field(default_factory=list)

    # --- reserved instances

    @property
    def reserved_count(self) -> int:
        return sum(p.count for p in self.pools)

    @property
    def matched_count(self) -> int:
        return sum(p.matched for p in self.pools)

    @property
    def unused_reserved(self) -> int:
        return sum(p.unused for p in self.pools)

    @property
    def unused_reserved_hours(self) -> Decimal:
        return Decimal(self.unused_reserved) * pricing.HOURS_PER_MONTH

    @property
    def ri_utilization_pct(self) -> Decimal:
        """Share of reserved capacity that a running instance is sitting on."""
        if not self.reserved_count:
            return Decimal(0)
        return Decimal(self.matched_count) / Decimal(self.reserved_count) * 100

    @property
    def ri_coverage_pct(self) -> Decimal:
        """Share of running instances an RI is discounting."""
        if not self.running:
            return Decimal(0)
        return Decimal(self.matched_count) / Decimal(len(self.running)) * 100

    @property
    def uncovered_hours(self) -> Decimal:
        return Decimal(len(self.uncovered)) * pricing.HOURS_PER_MONTH

    # --- savings plans

    @property
    def uncovered_hourly(self) -> Decimal:
        """On-demand $/hr no RI is discounting — what a Savings Plan would take."""
        return sum((r.hourly for r in self.uncovered), Decimal(0))

    @property
    def uncovered_monthly(self) -> Decimal:
        return self.uncovered_hourly * pricing.HOURS_PER_MONTH

    @property
    def committed_hourly(self) -> Decimal:
        return sum((p.commitment for p in self.plans), Decimal(0))

    @property
    def sp_unused_hourly(self) -> Decimal:
        """Committed spend with no eligible usage under it — direct waste."""
        return max(Decimal(0), self.committed_hourly - self.uncovered_hourly)

    @property
    def sp_unused_monthly(self) -> Decimal:
        return self.sp_unused_hourly * pricing.HOURS_PER_MONTH

    @property
    def sp_utilization_pct(self) -> Decimal:
        if self.committed_hourly <= 0:
            return Decimal(0)
        used = min(self.committed_hourly, self.uncovered_hourly)
        return used / self.committed_hourly * 100

    @property
    def sp_coverage_pct(self) -> Decimal:
        """Committed share of eligible compute spend."""
        total = self.committed_hourly + self.uncovered_hourly
        if total <= 0:
            return Decimal(0)
        return self.committed_hourly / total * 100


def build(provider: Provider, *, ttl: float = _TTL) -> Inventory:
    """Join the account's instances, RIs and Savings Plans. Briefly cached."""
    now = time.monotonic()
    hit = _cache.get(provider.alias)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    inv = _build(provider)
    _cache[provider.alias] = (now, inv)
    return inv


def clear_cache() -> None:
    _cache.clear()


def _build(provider: Provider) -> Inventory:
    running = for_each_region(
        provider, lambda r: _instances(provider, r), what="inventory instances"
    )
    pools = for_each_region(
        provider, lambda r: _reserved(provider, r), what="inventory reserved"
    )
    plans = _savings_plans(provider)
    uncovered = _match(running, pools)
    return Inventory(running=running, pools=pools, plans=plans, uncovered=uncovered)


def _instances(provider: Provider, region: str) -> list[Running]:
    ec2 = provider.client("ec2", region)
    out: list[Running] = []
    for page in ec2.get_paginator("describe_instances").paginate():
        for reservation in page.get("Reservations", []):
            for raw in reservation.get("Instances", []):
                inst = _Instance.model_validate(raw)
                # spot is already discounted and can't be committed to
                if inst.state != "running" or inst.lifecycle == "spot":
                    continue
                out.append(Running(
                    instance_id=inst.instance_id,
                    instance_type=inst.instance_type,
                    region=region,
                    az=inst.availability_zone,
                ))
    return out


def _reserved(provider: Provider, region: str) -> list[ReservedPool]:
    ec2 = provider.client("ec2", region)
    out: list[ReservedPool] = []
    for raw in ec2.describe_reserved_instances().get("ReservedInstances", []):
        ri = _ReservedInstance.model_validate(raw)
        if ri.state != "active" or ri.instance_count <= 0:
            continue
        regional = ri.scope != "Availability Zone"
        out.append(ReservedPool(
            instance_type=ri.instance_type,
            region=region,
            az="" if regional else ri.availability_zone,
            count=ri.instance_count,
        ))
    return out


def _savings_plans(provider: Provider) -> list[_SavingsPlan]:
    """Active Savings Plans. Global endpoint, and the one grant most roles lack."""
    try:
        sp = provider.client("savingsplans", "us-east-1")
        raw = sp.describe_savings_plans(states=["active"]).get("savingsPlans", [])
    except Exception as exc:  # noqa: BLE001 - no grant / no plans must not sink the sweep
        log.info("savings plans unavailable for %s: %s", provider.alias, exc)
        return []
    plans = [_SavingsPlan.model_validate(p) for p in raw]
    # SageMaker/Database plans commit against usage we don't inventory; counting
    # them would read as a compute plan nobody is using
    return [p for p in plans if p.state == "active" and p.plan_type in _COMPUTE_PLANS]


def _match(running: list[Running], pools: list[ReservedPool]) -> list[Running]:
    """Assign running instances to RI pools, returning what's left uncovered.

    az-scoped pools bind first: a regional pool can absorb anything in its
    region, so letting it go first would strand the narrower pool as unused.
    """
    free = list(running)
    for pool in sorted(pools, key=lambda p: not p.az):
        for inst in list(free):
            if pool.matched >= pool.count:
                break
            if inst.instance_type != pool.instance_type:
                continue
            out_of_scope = (
                inst.az != pool.az if pool.az else inst.region != pool.region
            )
            if out_of_scope:
                continue
            pool.matched += 1
            free.remove(inst)
    return free
