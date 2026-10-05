"""The on-prem collector: one inventory pass, one rate card per pool, `CostRecord`s out.

This is the join between the two halves that already exist — `inventory.py` measures a
pool, `config.py` prices it, `rates.allocate()` divides one by the other — and it emits
the same `CostRecord` the aws collectors do, so the digest, spike, forecast, budget and
showback detectors work on own iron with no changes at all.

Four decisions live here, and they are the ones that make the numbers add up:

* **a day's run-rate, stamped on one day**, like `public_ipv4`: the pass sees *now*, and
  the detectors key on `period.end`. A day is `24/730` of the monthly pool, never 1/30 or
  1/31 — the 730-hour month is what the printed $/vcpu-hour is derived from, so any other
  divisor would make the two disagree
* **one record per vm**, because showback over own iron is the whole point: the vm is the
  thing an operator can attribute, and `dimensions` carries its cluster and host
* **the leftover pool cost is emitted too**, as a `headroom` record. The operator pays for
  the whole cluster whether it is full or not, so without that line the site's total spend
  would be the sum of its vms and quietly under-report the real bill
* **a pool that cannot be priced is skipped, not fatal.** One cluster with no datastore
  mounted must not blank the spend for the rest of the site; but if *every* pool fails
  the collector raises, because a $0.00 report is worse than an error

Measured usage is not here yet (see `inventory.py`), so `allocate()` charges provisioned
and the waste column is zero. That is the honest shape until the perf-counter pass lands:
the records are real spend, just not yet split into used and wasted.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from clont.core.errors import ConfigError
from clont.core.logging import get_logger
from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.models import CostRecord, Recommendation
from clont.finops.onprem.rates import HOURS_PER_MONTH, allocate
from clont.providers.onprem.inventory import Pool, SiteInventory
from clont.providers.onprem.provider import OnPremProvider

log = get_logger("clont.finops.onprem")

_USD = "USD"
_SERVICE = "onprem"
_VM = "vm"
_HEADROOM = "headroom"
# the month the rates are quoted against, so a day is this share of it
_DAY_SHARE = Decimal(24) / Decimal(HOURS_PER_MONTH)
_CENT = Decimal("0.01")


@register("finops", Cloud.ONPREM, _SERVICE)
class OnPremCostCollector:
    cloud = Cloud.ONPREM
    service = _SERVICE
    # a vcenter pass is free and cheap, but nothing in it moves by the minute
    collect_every_seconds = 3600
    recommend_every_seconds = 86400

    def __init__(self, provider: OnPremProvider, tuning=None) -> None:
        self._provider = provider

    def collect(self, period: Period) -> list[CostRecord]:
        site = self._provider.inventory()
        day = period.end
        records: list[CostRecord] = []
        failed: list[str] = []
        for pool in site.pools:
            try:
                records.extend(self._pool_records(pool, day))
            except ConfigError as exc:
                # a pool with no capacity is a failed measurement, not free iron
                log.warning("%s: pool %s not priced: %s", self._provider.alias, pool.key, exc)
                failed.append(pool.key)
        if failed and not records:
            raise ConfigError(f"no pool could be priced: {', '.join(failed)}")
        self._log_leftovers(site)
        return records

    def recommendations(self, period: Period) -> list[Recommendation]:
        # idle, orphan and rightsizing advice needs measured usage, and the pass has none
        # yet — a finding off provisioned alone would just be a list of every vm
        return []

    def _pool_records(self, pool: Pool, day: date) -> list[CostRecord]:
        card = self._card(pool)
        result = allocate(pool.allocation_payload(card))
        when = Period(start=day, end=day)
        shared = {"cluster": pool.key, "pool_kind": pool.kind}

        records = [
            CostRecord(
                cloud=str(Cloud.ONPREM),
                service=_VM,
                period=when,
                alias=self._provider.alias,
                cost=self._day_cost(result["vms"][vm.name]["provisioned"]),
                resource=CloudResource(
                    cloud=Cloud.ONPREM,
                    service=_VM,
                    resource_id=vm.name,
                    region=pool.key,
                    alias=self._provider.alias,
                ),
                dimensions=shared
                | {
                    "host": vm.host or "",
                    "state": _state(vm),
                    "vcpu": str(vm.vcpu),
                    "ram_gib": _num(vm.ram_gib),
                    "disk_gib": _num(vm.disk_gib),
                },
            )
            for vm in pool.vms
        ]
        records.append(
            CostRecord(
                cloud=str(Cloud.ONPREM),
                service=_HEADROOM,
                period=when,
                alias=self._provider.alias,
                # negative headroom is an overcommitted pool: the vms already carry more
                # than the pool costs, so there is nothing left to bill for
                cost=self._day_cost(max(result["headroom"], 0.0)),
                dimensions=shared | _pool_dimensions(pool, result),
            )
        )
        return records

    def _card(self, pool: Pool) -> dict:
        """The rate card for one pool. A qualified name wins over a bare one.

        Two datacenters behind one vcenter may each hold a "prod" cluster, so an operator
        who has to tell them apart writes `DC0/prod` in `clusters:` and that entry is used
        for it alone.
        """
        site = self._provider.site
        return site.pool(pool.key if pool.key in site.clusters else pool.name)

    def _day_cost(self, monthly: float) -> Money:
        # allocate() hands back floats; money is Decimal, and str() keeps the float's
        # binary error out of it. cents, like every other derived figure here — the
        # division has 28 digits and a slack message should not carry them. the pool's
        # own total can land a cent or two off the sum of its lines; an invoice does too
        day = Decimal(str(monthly)) * _DAY_SHARE
        return Money(amount=day.quantize(_CENT, rounding=ROUND_HALF_UP), currency=_USD)

    def _log_leftovers(self, site: SiteInventory) -> None:
        """What belongs to no pool. Findings later, a line in the log now."""
        if site.orphan_vms:
            log.info(
                "%s: %d vm(s) on no host: %s",
                self._provider.alias,
                len(site.orphan_vms),
                ", ".join(vm.name for vm in site.orphan_vms),
            )
        if site.unmounted_datastores:
            log.info(
                "%s: %d datastore(s) nothing mounts: %s",
                self._provider.alias,
                len(site.unmounted_datastores),
                ", ".join(site.unmounted_datastores),
            )


def _pool_dimensions(pool: Pool, result: dict) -> dict[str, str]:
    """Everything the report needs to defend a number: the rates, and what produced them.

    The weights are arguable by design, so they travel next to the rates they split rather
    than living only in the operator's yaml.
    """
    rates = result["rates"]
    weights = result["weights"]
    overcommit = result["overcommit"]
    return {
        "pool_monthly": _num(result["pool_monthly"]),
        "rate_vcpu_hour": _num(rates["vcpu_hour"]),
        "rate_ram_gib_hour": _num(rates["ram_gib_hour"]),
        "rate_storage_gib_month": _num(rates["storage_gib_month"]),
        "weights": ",".join(f"{key}={_num(value)}" for key, value in weights.items()),
        "capacity_vcpu": _num(result["capacity"]["vcpu"]),
        "capacity_ram_gib": _num(result["capacity"]["ram_gib"]),
        "capacity_storage_gib": _num(result["capacity"]["storage_gib"]),
        "overcommit_vcpu": _num(overcommit["vcpu"]),
        "overcommit_ram": _num(overcommit["ram"]),
        "overcommit_storage": _num(overcommit["storage"]),
        "allocated_ratio": _num(result["allocated_ratio"]),
        "hosts": str(len(pool.hosts)),
        "hosts_powered_off": ",".join(pool.hosts_powered_off),
        "vms": str(len(pool.vms)),
        "incomplete_vms": ",".join(pool.incomplete_vms),
        "shared_datastores": ",".join(pool.shared_datastores),
    }


def _state(vm) -> str:
    if vm.template:
        return "template"
    return "running" if vm.powered_on else "stopped"


def _num(value: float | Decimal) -> str:
    # dimensions are strings; keep them readable, they end up in a slack message
    return f"{float(value):.6g}"
