"""What one inventory pass is paying for and not using.

Most of it holds on a single snapshot and needs no metrics at all. Two kinds do —
`idle-vm` and `rightsize-vm` — and they are only emitted for the vms the perf pass could
measure (`providers/onprem/metrics.py`): with no measurement a provisioned-only finding
is a list of every vm, so a vm with too little history is simply not advised about.

What the snapshot alone proves:

* **a powered-off vm** still owns its disk, and nothing else: its ram went back to the
  pool. the saving is the space it *occupies* — deleting a thin disk gives back the blocks
  it wrote, not the size it was promised
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

And what the measured window adds:

* **an idle vm** is powered on and ran at nothing over the whole window — on cpu *and*
  ram, because a cache does 2% cpu at 90% ram and switching it off is not advice. The
  saving is its cpu and ram share only: its disk survives a shutdown
* **an oversized vm** is busy enough to keep, with more vcpu or ram than its p95 ever
  needed. The saving is what it hands back after `onprem_rightsize_target_pct` of
  headroom is left on top of the peak — so a vm already running near that target yields
  nothing and emits no finding, which is the gate rather than a second threshold

Three rules about the money:

* **per-pool findings are priced on the operator's own card**, so they are exact, not a
  ballpark — `approximate=False`, which is unusual for a clont recommendation and is the
  point of the on-prem model
* **site-wide findings have no pool** (an unmounted array is in no cluster, an orphan vm
  is on no host), so they are priced at the site's mean $/GiB-month — total storage money
  over total capacity — and they say so with `approximate=True`
* **what a vm hands back is divided by the pool's oversubscription.** The rates split the
  pool over *capacity*, so one allocated vcpu is priced as if the cluster were exactly
  full; at 4:1 — an ordinary vmware ratio — switching off a 4-vcpu vm frees one physical
  core, not four. Measured on 1250 real vms (test layer 3), the unscaled sum advised
  $142k/month of savings on a cluster that costs $108k, and a report cannot offer back
  more than the whole bill. A host finding is *not* scaled: a host is the iron
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from clont.core.errors import ConfigError
from clont.core.logging import get_logger
from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.base import FinOpsTuning
from clont.finops.models import CostRecord, Recommendation
from clont.finops.onprem.rates import allocate
from clont.providers.onprem.inventory import Pool, SiteInventory, Vm
from clont.providers.onprem.metrics import Usage
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
    # what makes it the same thing twice — a moref, never the shown name. vcenter happily
    # holds two `web-01` and two `LocalDS_0`, and `_once` would drop one of them
    uid: str = ""


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
                result = allocate(pool.allocation_payload(self._card(pool), site.usage))
            except ConfigError as exc:
                # same rule as the cost pass: one unpriceable cluster is not the floor
                log.warning("%s: pool %s not priced: %s", self._provider.alias, pool.key, exc)
                continue
            priced.append((pool, result))
            findings.extend(pool_findings(pool, result, self._tuning))
            findings.extend(usage_findings(pool, result, site.usage, self._tuning))

        rate = site_storage_rate(priced)
        if rate is None:
            # nothing to price the leftovers with, and a GiB figure with no dollars on it
            # would just read as a bug in the report
            log.warning("%s: no pool priced, site-wide findings skipped", self._provider.alias)
        else:
            findings.extend(site_findings(site, rate, self._tuning))
        return [self._rec(finding) for finding in _once(findings)]

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


def _once(findings: Iterable[Finding]) -> list[Finding]:
    """One finding per thing. A shared san is in every pool that mounts it, and a cluster
    cannot fix it twice — the first pool keeps it, so the report says the array, not the
    mount count.

    Keyed on the moref and not on the shown name: two datacenters each hold a `LocalDS_0`
    and a `web-01`, and a name key called them one thing and silently dropped a real
    finding — the second cluster's stopped vm, its dead array, its thin risk.
    """
    seen: set[tuple[str, str]] = set()
    out: list[Finding] = []
    for finding in findings:
        key = (finding.kind, finding.uid or finding.resource_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(finding)
    return out


def pool_findings(pool: Pool, priced: dict, tuning: FinOpsTuning) -> list[Finding]:
    """What one priced pool is paying for and not using. `priced` is `allocate()`'s answer."""
    storage_rate = _handback_rate(priced, "storage_gib_month", "storage")
    floor = Decimal(str(tuning.onprem_min_savings_usd))
    out: list[Finding] = []

    labels = pool.labels()
    for vm in pool.vms:
        if vm.powered_on or vm.committed_gib <= 0:
            continue
        # deleting it gives back the blocks it wrote, never the thin promise
        monthly = vm.committed_gib * storage_rate
        if monthly < floor:
            continue
        out.append(
            Finding(
                kind="template-disk" if vm.template else "stopped-vm",
                resource_id=labels[vm.uid],
                region=pool.key,
                summary=_disk_summary(vm),
                monthly=monthly,
                uid=vm.uid,
            )
        )

    out.extend(_host_findings(pool, priced, floor))
    out.extend(_thin_findings(pool, tuning))
    return out


def usage_findings(
    pool: Pool, priced: dict, usage: dict[str, Usage], tuning: FinOpsTuning
) -> list[Finding]:
    """Idle and oversized vms, for the vms the perf pass measured and nobody else.

    One vm produces at most one of the two: an idle vm is advised to be switched off, and
    telling an operator to shrink a machine they are about to delete is noise.
    """
    hours = Decimal(str(priced["hours_per_month"]))
    cpu_rate = _handback_rate(priced, "vcpu_hour", "vcpu") * hours
    ram_rate = _handback_rate(priced, "ram_gib_hour", "ram") * hours
    floor = Decimal(str(tuning.onprem_min_savings_usd))
    idle_cpu = Decimal(str(tuning.idle_cpu_pct))
    idle_ram = Decimal(str(tuning.onprem_idle_ram_pct))
    target = Decimal(str(tuning.onprem_rightsize_target_pct)) / _HUNDRED

    labels = pool.labels()
    out: list[Finding] = []
    for vm in pool.vms:
        row = usage.get(vm.uid)
        if row is None or not vm.running:
            continue
        monthly = Decimal(vm.vcpu) * cpu_rate + vm.ram_gib * ram_rate
        if row.cpu_pct <= idle_cpu and row.ram_pct <= idle_ram:
            if monthly < floor:
                continue
            out.append(
                Finding(
                    kind="idle-vm",
                    resource_id=labels[vm.uid],
                    region=pool.key,
                    summary=(
                        f"{vm.name} ran at {row.cpu_pct:.1f}% cpu and {row.ram_pct:.1f}% ram "
                        f"(p95 over {row.samples} samples) — switching it off returns "
                        f"{vm.vcpu} vcpu and {_gib(vm.ram_gib)} GiB to the pool. its "
                        f"{_gib(vm.committed_gib)} GiB of disk stays, so that part is not in "
                        "the saving"
                    ),
                    monthly=monthly,
                    uid=vm.uid,
                )
            )
            continue
        fit = _rightsize(vm, row, target)
        if fit is None:
            continue
        vcpu, ram_gib = fit
        saving = Decimal(vm.vcpu - vcpu) * cpu_rate + (vm.ram_gib - ram_gib) * ram_rate
        if saving < floor:
            continue
        out.append(
            Finding(
                kind="rightsize-vm",
                resource_id=labels[vm.uid],
                region=pool.key,
                summary=(
                    f"{vm.name} peaked at {_gib(row.vcpu)} vcpu and {_gib(row.ram_gib)} GiB "
                    f"(p95 over {row.samples} samples) on {vm.vcpu} vcpu / "
                    f"{_gib(vm.ram_gib)} GiB — {vcpu} vcpu / {_gib(ram_gib)} GiB leaves the "
                    f"peak at {tuning.onprem_rightsize_target_pct:.0f}% of the new size"
                ),
                monthly=saving,
                uid=vm.uid,
            )
        )
    return out


def _rightsize(vm: Vm, row: Usage, target: Decimal) -> tuple[int, Decimal] | None:
    """The smallest whole vcpu / whole GiB that leaves the p95 at `target` of it.

    Rounded up, and never below one vcpu or one GiB: a vm that fits in nothing is still a
    vm. None when neither number moves — then there is nothing to advise.
    """
    vcpu = max(1, int(_ceil_decimal(row.vcpu / target)))
    ram_gib = max(Decimal(1), _ceil_decimal(row.ram_gib / target))
    vcpu = min(vcpu, vm.vcpu)
    ram_gib = min(ram_gib, vm.ram_gib)
    if vcpu == vm.vcpu and ram_gib == vm.ram_gib:
        return None
    return vcpu, ram_gib


def _ceil_decimal(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding=ROUND_CEILING)


def _handback_rate(priced: dict, rate: str, dimension: str) -> Decimal:
    """What one unit a *vm* gives back is worth, on a pool that may be oversubscribed.

    `allocate()` divides the pool over capacity, so the rate assumes the cluster is
    exactly full. Dividing by the oversubscription turns an allocated unit back into the
    iron it really occupies; a pool with slack has a ratio under 1 and is left alone,
    because the empty half is headroom and not a discount.
    """
    oversubscribed = max(Decimal(str(priced["overcommit"][dimension])), Decimal(1))
    return Decimal(str(priced["rates"][rate])) / oversubscribed


def site_findings(
    site: SiteInventory, storage_rate: Decimal, tuning: FinOpsTuning
) -> list[Finding]:
    """The leftovers that belong to no pool, priced at the site's mean storage rate."""
    floor = Decimal(str(tuning.onprem_min_savings_usd))
    out: list[Finding] = []

    for vm in site.orphan_vms:
        monthly = vm.committed_gib * storage_rate
        if monthly < floor:
            continue
        out.append(
            Finding(
                kind="orphan-vm",
                resource_id=vm.name,
                region=_SITE,
                summary=(
                    f"vcenter lists {vm.name} on no host, holding "
                    f"{_gib(vm.committed_gib)} GiB — check it is not a leftover of a "
                    "failed migration"
                ),
                monthly=monthly,
                approximate=True,
                uid=vm.uid,
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
                uid=datastore.uid,
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
    """The site's mean $/GiB-month: all the pools' storage money over the GiB it buys.

    Weighted by capacity rather than averaged over pools, so a 10 TiB array is never
    priced by a tiny cluster's rate. The divisor counts each array **once, by moref**: a
    san mounted by three clusters sits in three pools, and summing the pools' capacity
    would count it three times and leave the rate a third of the real one. Arrays nobody
    mounts stay out of it — no pool's card is paying for their GiB. None when no pool
    could be priced at all.
    """
    money = Decimal(0)
    arrays: dict[str, Decimal] = {}
    for pool, result in priced:
        gib = Decimal(str(result["capacity"]["storage_gib"]))
        money += Decimal(str(result["rates"]["storage_gib_month"])) * gib
        for datastore in pool.datastores:
            arrays[datastore.uid] = datastore.capacity_gib
    capacity = sum(arrays.values(), Decimal(0))
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
                uid=f"{pool.key}/{host.name}",
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
            uid=datastore.uid,
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

    An array nobody mounts is left out: `unmounted-datastore` already offers its whole
    capacity back, so counting the space on it again would price the same gib twice and
    bury the real leftovers on the live arrays under it.
    """
    dead = {ds.uid for ds in site.unmounted_datastores}
    used = sum(
        (ds.capacity_gib - ds.free_gib for ds in site.datastores if ds.uid not in dead),
        Decimal(0),
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
    promised = (
        f", {_gib(vm.disk_gib)} GiB promised" if vm.disk_gib > vm.committed_gib else ""
    )
    return (
        f"{vm.name} is {what}, still occupying {_gib(vm.committed_gib)} GiB"
        f"{promised}. its ram is back in the pool, its disk is not"
    )


def _gib(value: Decimal) -> str:
    return f"{float(value):,.0f}"
