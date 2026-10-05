"""Waste one inventory pass can prove — no metrics, no thresholds on a trend.

The aws side splits the same way: `waste.py` / `snapshots.py` name things that are
provably paid for and carrying nothing, while `idle.py` needs a metric window. On own
iron the split matters more, because the pass has no measured usage yet (see
`providers/onprem/inventory.py`), so **idle and rightsizing are not here** — provisioned
alone would call every vm idle. What is here holds on a single snapshot:

* **a powered-off vm** still owns its disk, and nothing else: its ram went back to the pool
* **a template** is a powered-off vm nobody is going to boot, so it is its own line — an
  operator who keeps a golden image on purpose can ignore the kind wholesale
* **a zombie host** is powered on, in the cluster, running nothing. its share of the pool
  is what the operator pays for iron that carries no load
* **a powered-off host** is the same bill minus the power, and it is *not* a discount:
  capacity counts installed iron, so the cluster's rates already charge for it
* **storage no vm accounts for** — datastore used space minus what every vm claims. this
  is the read-only twin of aws's unattached volume: clont runs under the vsphere
  Read-Only role, which cannot browse a datastore, so we can prove the *size* of the
  leftovers (isos, orphaned vmdks, dead folders) but never list the files
* **thin overcommit** is a risk, not a saving: it reports $0 and says what the promises
  add up to

Two rules about the money:

* **per-pool findings are priced on the operator's own card**, so they are exact, not a
  ballpark — `approximate=False`, which is unusual for a clont recommendation and is the
  point of the on-prem model
* **site-wide findings have no pool** (an unmounted array is in no cluster, an orphan vm
  is on no host), so they are priced at the site's mean $/GiB-month — total storage money
  over total capacity — and they say so with `approximate=True`
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from clont.core.errors import ConfigError
from clont.core.logging import get_logger
from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.base import FinOpsTuning
from clont.finops.models import CostRecord, Recommendation
from clont.finops.onprem.rates import allocate
from clont.providers.onprem.inventory import Pool, SiteInventory, Vm
from clont.providers.onprem.provider import OnPremProvider

log = get_logger("clont.finops.onprem.waste")

_USD = "USD"
_SERVICE = "waste"
_SITE = "site"  # the "region" of a finding that belongs to no pool
_CENT = Decimal("0.01")
_HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing to delete or switch off, priced per month."""

    kind: str
    resource_id: str
    region: str       # the pool it was found in, or "site"
    summary: str
    monthly: Decimal  # usd, 0 for a risk that has no saving
    approximate: bool = False


@register("finops", Cloud.ONPREM, _SERVICE)
class OnPremWasteCollector:
    cloud = Cloud.ONPREM
    service = _SERVICE
    # `collect()` is empty: the spend lines are `finops/onprem/costs.py`'s job, this one
    # only ever advises. nothing it looks at moves by the hour
    collect_every_seconds = 86400
    recommend_every_seconds = 86400

    def __init__(self, provider: OnPremProvider, tuning: FinOpsTuning | None = None) -> None:
        self._provider = provider
        self._tuning = tuning or FinOpsTuning()

    def collect(self, period: Period) -> list[CostRecord]:
        return []

    def recommendations(self, period: Period) -> list[Recommendation]:
        site = self._provider.inventory()
        findings: list[Finding] = []
        priced: list[tuple[Pool, dict]] = []
        for pool in site.pools:
            try:
                result = allocate(pool.allocation_payload(self._card(pool)))
            except ConfigError as exc:
                # same rule as the cost pass: one unpriceable cluster is not the floor
                log.warning("%s: pool %s not priced: %s", self._provider.alias, pool.key, exc)
                continue
            priced.append((pool, result))
            findings.extend(pool_findings(pool, result, self._tuning))

        rate = site_storage_rate(priced)
        if rate is None:
            # nothing to price the leftovers with, and a GiB figure with no dollars on it
            # would just read as a bug in the report
            log.warning("%s: no pool priced, site-wide findings skipped", self._provider.alias)
        else:
            findings.extend(site_findings(site, rate, self._tuning))
        return [self._rec(finding) for finding in findings]

    def _card(self, pool: Pool) -> dict:
        return self._provider.site.card_for(pool.key, pool.name)

    def _rec(self, finding: Finding) -> Recommendation:
        return Recommendation(
            cloud=str(Cloud.ONPREM),
            service=_SERVICE,
            kind=finding.kind,
            resource=CloudResource(
                cloud=Cloud.ONPREM,
                service=_SERVICE,
                resource_id=finding.resource_id,
                region=finding.region,
                alias=self._provider.alias,
            ),
            summary=finding.summary,
            estimated_savings=Money(
                amount=finding.monthly.quantize(_CENT, rounding=ROUND_HALF_UP), currency=_USD
            ),
            priced_region=finding.region,
            approximate=finding.approximate,
        )


def pool_findings(pool: Pool, priced: dict, tuning: FinOpsTuning) -> list[Finding]:
    """What one priced pool is paying for and not using. `priced` is `allocate()`'s answer."""
    storage_rate = Decimal(str(priced["rates"]["storage_gib_month"]))
    floor = Decimal(str(tuning.onprem_min_savings_usd))
    out: list[Finding] = []

    for vm in pool.vms:
        if vm.powered_on or vm.disk_gib <= 0:
            continue
        monthly = vm.disk_gib * storage_rate
        if monthly < floor:
            continue
        out.append(
            Finding(
                kind="template-disk" if vm.template else "stopped-vm",
                resource_id=vm.name,
                region=pool.key,
                summary=_disk_summary(vm),
                monthly=monthly,
            )
        )

    out.extend(_host_findings(pool, priced, floor))
    out.extend(_thin_findings(pool, tuning))
    return out


def site_findings(
    site: SiteInventory, storage_rate: Decimal, tuning: FinOpsTuning
) -> list[Finding]:
    """The leftovers that belong to no pool, priced at the site's mean storage rate."""
    floor = Decimal(str(tuning.onprem_min_savings_usd))
    out: list[Finding] = []

    for vm in site.orphan_vms:
        monthly = vm.disk_gib * storage_rate
        if monthly < floor:
            continue
        out.append(
            Finding(
                kind="orphan-vm",
                resource_id=vm.name,
                region=_SITE,
                summary=(
                    f"vcenter lists {vm.name} on no host, holding "
                    f"{_gib(vm.disk_gib)} GiB — check it is not a leftover of a failed migration"
                ),
                monthly=monthly,
                approximate=True,
            )
        )

    for datastore in site.unmounted_datastores:
        monthly = datastore.capacity_gib * storage_rate
        if monthly < floor:
            continue
        out.append(
            Finding(
                kind="unmounted-datastore",
                resource_id=datastore.name,
                region=_SITE,
                summary=(
                    f"no host mounts {datastore.name}, so none of its "
                    f"{_gib(datastore.capacity_gib)} GiB can be used — unmount it, "
                    "or give it to a cluster"
                ),
                monthly=monthly,
                approximate=True,
            )
        )

    unaccounted = _unaccounted_gib(site, tuning)
    if unaccounted is not None and unaccounted * storage_rate >= floor:
        out.append(
            Finding(
                kind="unaccounted-storage",
                resource_id="datastore-leftovers",
                region=_SITE,
                summary=(
                    f"{_gib(unaccounted)} GiB of datastore space no vm accounts for — "
                    "isos, orphaned vmdks or dead vm folders. clont reads vcenter under the "
                    "Read-Only role and cannot browse a datastore, so this is the size, "
                    "not the file list"
                ),
                monthly=unaccounted * storage_rate,
                approximate=True,
            )
        )
    return out


def site_storage_rate(priced: Iterable[tuple[Pool, dict]]) -> Decimal | None:
    """The site's mean $/GiB-month: every pool's storage money over every pool's capacity.

    Weighted by capacity rather than averaged over pools, so a 10 TiB array does not get
    priced by a tiny cluster's rate. None when no pool could be priced at all.
    """
    money = Decimal(0)
    capacity = Decimal(0)
    for _pool, result in priced:
        gib = Decimal(str(result["capacity"]["storage_gib"]))
        money += Decimal(str(result["rates"]["storage_gib_month"])) * gib
        capacity += gib
    return money / capacity if capacity > 0 else None


def _host_findings(pool: Pool, priced: dict, floor: Decimal) -> list[Finding]:
    """Iron carrying no load. Its share of the pool is cpu + ram, never storage.

    The datastores stay whether the host does or not, so charging a host for them would
    double-count the arrays the rest of the cluster still mounts.
    """
    capacity = priced["capacity"]
    weights = priced["weights"]
    pool_monthly = Decimal(str(priced["pool_monthly"]))
    busy = {vm.host for vm in pool.vms if vm.running}

    out: list[Finding] = []
    for host in pool.hosts:
        if host.powered_on and host.name in busy:
            continue
        share = Decimal(str(weights["cpu"])) * Decimal(host.cores) / Decimal(
            str(capacity["vcpu"])
        ) + Decimal(str(weights["ram"])) * host.ram_gib / Decimal(str(capacity["ram_gib"]))
        monthly = pool_monthly * share
        if monthly < floor:
            continue
        iron = f"{host.cores} cores / {_gib(host.ram_gib)} GiB in {pool.name}"
        state = (
            f"is powered on with no vm running on it — {iron}, burning power for nothing"
            if host.powered_on
            else f"is switched off but still in the cluster — {iron}, "
            "still in the capex and still licensed"
        )
        out.append(
            Finding(
                kind="zombie-host" if host.powered_on else "powered-off-host",
                resource_id=host.name,
                region=pool.key,
                summary=f"{host.name} {state}",
                monthly=monthly,
            )
        )
    return out


def _thin_findings(pool: Pool, tuning: FinOpsTuning) -> list[Finding]:
    """Thin provisioning promising more than the array has. A risk, so it prices at zero."""
    limit = Decimal(str(tuning.onprem_thin_overcommit_ratio))
    return [
        Finding(
            kind="thin-overcommit",
            resource_id=datastore.name,
            region=pool.key,
            summary=(
                f"{datastore.name} is thin-provisioned {datastore.overcommit:.2f}x: "
                f"{_gib(datastore.provisioned_gib)} GiB promised on "
                f"{_gib(datastore.capacity_gib)} GiB, {_gib(datastore.free_gib)} GiB free — "
                "it fills before the vms notice"
            ),
            monthly=Decimal(0),
        )
        for datastore in pool.datastores
        if datastore.overcommit >= limit
    ]


def _unaccounted_gib(site: SiteInventory, tuning: FinOpsTuning) -> Decimal | None:
    """Used datastore space minus what every vm claims, once both thresholds are passed.

    Summed over the site's flat datastore list on purpose: a shared array sits in every
    pool that mounts it, so a per-pool subtraction would charge one cluster for another
    cluster's vms. A negative gap is normal on an array that dedupes or compresses — the
    vms claim more than the disks hold — and is not a finding.
    """
    used = sum(
        (ds.capacity_gib - ds.free_gib for ds in site.datastores), Decimal(0)
    )
    claimed = sum((vm.committed_gib for vm in site.vms()), Decimal(0))
    gap = used - claimed
    if used <= 0 or gap < Decimal(str(tuning.onprem_unaccounted_min_gib)):
        return None
    if gap / used * _HUNDRED < Decimal(str(tuning.onprem_unaccounted_min_pct)):
        return None
    return gap


def _disk_summary(vm: Vm) -> str:
    what = "a template" if vm.template else "powered off"
    # vcenter carries no power-off timestamp as a property, only as an event, so the
    # pass cannot say for how long — do not imply it can
    return (
        f"{vm.name} is {what}, still holding {_gib(vm.disk_gib)} GiB "
        f"({_gib(vm.committed_gib)} GiB written). its ram is back in the pool, its disk is not"
    )


def _gib(value: Decimal) -> str:
    return f"{float(value):,.0f}"
