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
  operator who keeps a golden image on purpose can ignore the kind wholesale. on a
  vcenter the clone events add when it was last cloned, or that it never was over the
  history the server still holds. a template younger than that history, a host agent
  (no history) or a failed read get no verdict at all
* **a zombie host** is powered on, in the cluster, running nothing. its share of the pool
  is what the operator pays for iron that carries no load
* **a powered-off host** is the same bill minus the power, and it is *not* a discount:
  capacity counts installed iron, so the cluster's rates already charge for it
* **storage no vm accounts for** — datastore used space minus what every vm claims. this
  is the read-only twin of aws's unattached volume: clont runs under the vsphere
  Read-Only role, which cannot browse a datastore, so we can prove the *size* of the
  leftovers (isos, orphaned vmdks, dead folders) but never list the files. a kubernetes
  cluster on this iron holds volumes that look exactly like that from the hypervisor, and
  it *can* name them — so whatever the clusters own up to comes off this gap and is
  reported against their claims instead
* **thin overcommit** is a risk, not a saving: it reports $0 and says what the promises
  add up to
* **an old vm snapshot** holds a delta that only grows, and slows every write. priced at
  the pool's storage rate on the size of its deltas, approximate because `layoutEx` sizes
  are what the files hold now. the advice is the snapshot manager, never the delta files:
  removing those by hand breaks the chain
* **consolidation needed** is vcenter saying a delete left deltas behind. a risk, $0
* **an orphaned vmdk / an unregistered vm folder** is the file list behind the storage
  gap, when the role has `Datastore.Browse` (`gc.py`). priced at the datastore's pool
  storage rate, approximate. whatever gc lists comes off `unaccounted-storage`, so the
  same GiB is never priced twice, and the gap that's left is logs, isos and other files
* **a cns leftover** is a vsphere csi volume whose cluster clont reads and whose pv is
  gone (`cns.py`), vcenter only. priced like an orphaned vmdk off its on-disk size, and
  it comes off `unaccounted-storage` the same way: a detached volume no pv claims is in
  that gap and in no cluster's claim, so without it the GiB would be priced twice

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
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from clont.core.errors import ConfigError
from clont.core.logging import get_logger
from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.base import FinOpsTuning
from clont.finops.guests import GuestStorage
from clont.finops.models import CostRecord, Recommendation
from clont.finops.onprem import cns, gc
from clont.finops.onprem.rates import allocate
from clont.providers.onprem.inventory import (
    CloneHistory,
    Pool,
    SiteInventory,
    Snapshot,
    Vm,
)
from clont.providers.onprem.metrics import Usage
from clont.providers.onprem.provider import OnPremProvider

log = get_logger("clont.finops.onprem.waste")

_USD = "USD"
_SERVICE = "waste"
_SITE = "site"  # the "region" of a finding that belongs to no pool
_CENT = Decimal("0.01")
_HUNDRED = Decimal(100)
# less event history than this proves nothing about a template, so no "never cloned"
CLONE_MIN_WINDOW_DAYS = 7

# what common backup tools name the snapshot they take, upper-cased. one left behind is a
# job that failed to clean up, and the operator should hear which tool
_BACKUP_SNAPSHOTS = (
    ("VEEAM BACKUP TEMPORARY SNAPSHOT", "veeam"),
    ("_VCB-BACKUP_", "vcb"),
    ("__GX_BACKUP__", "commvault"),
    ("NBU_SNAPSHOT", "netbackup"),
    ("NAKIVO", "nakivo"),
    ("RUBRIK", "rubrik"),
    ("COHESITY", "cohesity"),
    ("AVAMAR", "avamar"),
    ("CONSOLIDATE HELPER", "a failed consolidation"),
)


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
        site = self._site()
        now = datetime.now(UTC)
        findings: list[Finding] = []
        priced: list[tuple[Pool, dict]] = []
        ds_rates: dict[str, tuple[str, Decimal]] = {}
        for pool in site.pools:
            try:
                result = allocate(pool.allocation_payload(self._card(pool), site.usage))
            except ConfigError as exc:
                # same rule as the cost pass: one unpriceable cluster is not the floor
                log.warning("%s: pool %s not priced: %s", self._provider.alias, pool.key, exc)
                continue
            priced.append((pool, result))
            for datastore in pool.datastores:
                # a shared array goes to the first pool that prices it, as in `_once`
                ds_rates.setdefault(
                    datastore.uid,
                    (pool.key, _handback_rate(result, "storage_gib_month", "storage")),
                )
            findings.extend(pool_findings(pool, result, self._tuning, site.clones))
            findings.extend(usage_findings(pool, result, site.usage, self._tuning))
            findings.extend(
                snapshot_findings(
                    pool.vms,
                    pool.key,
                    _handback_rate(result, "storage_gib_month", "storage"),
                    self._tuning,
                    now,
                    pool.labels(),
                )
            )

        rate = site_storage_rate(priced)
        if rate is None:
            # nothing to price the leftovers with, and a GiB figure with no dollars on it
            # would just read as a bug in the report
            log.warning("%s: no pool priced, site-wide findings skipped", self._provider.alias)
        else:
            # what the clusters on this iron own up to holding, so the storage gap is not
            # offered back twice — see `_unaccounted_gib`
            findings.extend(
                site_findings(
                    site,
                    rate,
                    self._tuning,
                    self._guests(),
                    now=now,
                    ds_rates=ds_rates,
                    cns_clusters=self._cns_clusters(),
                )
            )
        return [self._rec(finding) for finding in _once(findings)]

    def _site(self) -> SiteInventory:
        # the browsed pass when the provider has one, gc needs the file list
        ask = getattr(self._provider, "browsed_inventory", None)
        return ask() if callable(ask) else self._provider.inventory()

    def _guests(self) -> list[GuestStorage]:
        """What the clusters on this site hold, or nothing when none is attached.

        Duck-typed: the collector is handed a provider and a backend with no guests to ask
        simply has none, which is the state every site was in before kubernetes was read.
        """
        ask = getattr(self._provider, "guest_storage", None)
        return list(ask()) if callable(ask) else []

    def _cns_clusters(self) -> dict[str, str]:
        inventory = getattr(self._provider.site, "inventory", None)
        return dict(getattr(inventory, "cns_clusters", None) or {})

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


def pool_findings(
    pool: Pool, priced: dict, tuning: FinOpsTuning, clones: CloneHistory | None = None
) -> list[Finding]:
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
                summary=_disk_summary(vm, clones),
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
    site: SiteInventory,
    storage_rate: Decimal,
    tuning: FinOpsTuning,
    guests: Iterable[GuestStorage] = (),
    *,
    now: datetime | None = None,
    ds_rates: dict[str, tuple[str, Decimal]] | None = None,
    cns_clusters: dict[str, str] | None = None,
) -> list[Finding]:
    """The leftovers that belong to no pool, priced at the site's mean storage rate.

    `ds_rates` is datastore moref -> (pool key, $/GiB-month) for the gc and cns rows; a
    datastore no priced pool mounts falls back to the site rate. `cns_clusters` is
    `inventory.cns_clusters`.
    """
    floor = Decimal(str(tuning.onprem_min_savings_usd))
    now = now or datetime.now(UTC)
    guests = list(guests)
    out: list[Finding] = snapshot_findings(site.orphan_vms, _SITE, storage_rate, tuning, now)

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

    swept = gc.sweep(site, tuning, now, keep=[p for g in guests for p in g.paths])
    if swept is not None:
        out.extend(gc_findings(swept, ds_rates or {}, storage_rate, now, floor=floor))

    verdict = cns.judge(site, guests, tuning, now, mapping=cns_clusters, swept=swept)
    if verdict is not None:
        _cns_log(verdict)
        out.extend(cns_findings(verdict, ds_rates or {}, storage_rate, now))

    gap = _unaccounted_gib(site, tuning, guests, swept, verdict)
    if gap is not None and gap.unexplained * storage_rate >= floor:
        if swept is None:
            what = (
                "— isos, orphaned vmdks or dead vm folders. clont reads vcenter under the "
                "Read-Only role and cannot browse a datastore, so this is the size, "
                "not the file list"
            )
        else:
            what = (
                "and no listed vmdk or vm folder explains — logs, isos, other files, and "
                "whatever sits on datastores gc could not judge"
            )
        out.append(
            Finding(
                kind="unaccounted-storage",
                resource_id="datastore-leftovers",
                region=_SITE,
                summary=(
                    f"{_gib(gap.unexplained)} GiB of datastore space no vm accounts for "
                    f"{what}{gap.credit}{gap.listed_credit}{gap.skipped_note}"
                ),
                monthly=gap.unexplained * storage_rate,
                approximate=True,
            )
        )
    return out


def gc_findings(
    swept: gc.Sweep,
    ds_rates: dict[str, tuple[str, Decimal]],
    storage_rate: Decimal,
    now: datetime,
    floor: Decimal = Decimal(0),
) -> list[Finding]:
    """One finding per unowned disk or unregistered folder, the path in the summary.

    Nothing is dropped under the savings floor, the gap they come off is priced in full
    either way. two or more orphaned disks under it on one datastore fold into one
    `small-orphans` row instead, so a pile of tiny leftovers is one line, not fifty.
    """
    out: list[Finding] = []
    small: dict[str, list[gc.Garbage]] = {}
    rows: list[gc.Garbage] = []
    for row in swept.garbage:
        rate = ds_rates.get(row.datastore.uid, (_SITE, storage_rate))[1]
        if row.kind == gc.ORPHANED_VMDK and row.size_gib * rate < floor:
            small.setdefault(row.datastore.uid, []).append(row)
        else:
            rows.append(row)
    for group in small.values():
        if len(group) == 1:
            rows.extend(group)
            continue
        ds = group[0].datastore
        region, rate = ds_rates.get(ds.uid, (_SITE, storage_rate))
        size = sum((g.size_gib for g in group), Decimal(0))
        shown = ", ".join(g.path for g in group[:5])
        more = f" and {len(group) - 5} more" if len(group) > 5 else ""
        out.append(
            Finding(
                kind=gc.ORPHANED_VMDK,
                resource_id=f"[{ds.name}] small-orphans",
                region=region,
                summary=(
                    f"{len(group)} small disks on {ds.name} no vm lists in its layout, "
                    f"~{_gib_fine(size)} GiB together: {shown}{more} — check nothing "
                    "outside this vcenter uses them, then delete them in the datastore browser"
                ),
                monthly=size * rate,
                approximate=True,
                uid=f"{ds.uid}:small-orphans",
            )
        )
    for row in rows:
        region, rate = ds_rates.get(row.datastore.uid, (_SITE, storage_rate))
        size = row.size_gib
        age = _age(now - row.modified)
        if row.kind == gc.UNREGISTERED_VM:
            vmx = next(f.path for f in row.files if f.path.lower().endswith(".vmx"))
            summary = (
                f"{row.path}/ holds {vmx.rsplit('/', 1)[-1]} and no vm in inventory points "
                f"at it — an unregistered vm, {len(row.files)} file(s), "
                f"~{_gib_fine(size)} GiB, last touched {age} ago. someone may have only "
                "removed it from inventory: re-register or delete the folder"
            )
        else:
            names = ", ".join(f.path.rsplit("/", 1)[-1] for f in row.files)
            summary = (
                f"{row.path} is a disk no vm lists in its layout ({names}), "
                f"~{_gib_fine(size)} GiB, last touched {age} ago — check nothing outside "
                "this vcenter uses it, then delete it in the datastore browser"
            )
        out.append(
            Finding(
                kind=row.kind,
                resource_id=row.path,
                region=region,
                summary=summary,
                monthly=size * rate,
                approximate=True,
                uid=f"{row.datastore.uid}:{row.path}",
            )
        )
    return out


def cns_findings(
    verdict: cns.Verdict,
    ds_rates: dict[str, tuple[str, Decimal]],
    storage_rate: Decimal,
    now: datetime,
) -> list[Finding]:
    """One finding per cns volume a known cluster dropped. no savings floor: the gap
    they come off is priced in full either way, and each one is someone's data."""
    out: list[Finding] = []
    for row in verdict.leftovers:
        volume = row.volume
        region, rate = ds_rates.get(row.datastore.uid, (_SITE, storage_rate))
        how = "learned from its pvs" if row.learned else "mapped in inventory.cns_clusters"
        recorded = [
            f"pv {', '.join(volume.named(cns.PV))}" if volume.named(cns.PV) else "",
            f"pvc {', '.join(volume.named(cns.PVC))}" if volume.named(cns.PVC) else "",
        ]
        said = " and ".join(part for part in recorded if part) or "no pv or pvc"
        name = f" ({volume.name})" if volume.name and volume.name != volume.id else ""
        out.append(
            Finding(
                kind=cns.CNS_LEFTOVER,
                resource_id=volume.id,
                region=region,
                summary=(
                    f"cns volume {volume.id}{name} on {row.datastore.name}, "
                    f"~{_gib_fine(row.size_gib)} GiB on disk of "
                    f"{_gib_fine(volume.capacity_gib)} GiB, belongs to container cluster "
                    f"{row.cluster} ({', '.join(row.owners)}, {how}) and no pv there "
                    f"points at it any more — cns still records {said}. last written "
                    f"{_age(now - row.modified)} ago. check nothing else uses it, then "
                    "delete it through the cluster or in the vsphere client (cloud native "
                    "storage > container volumes). never remove the vmdk by hand, cns "
                    "keeps its own record of it"
                ),
                monthly=row.size_gib * rate,
                approximate=True,
                uid=f"cns:{volume.id}",
            )
        )
    return out


def _cns_log(verdict: cns.Verdict) -> None:
    """At most two lines: the cluster ids nobody owns, and why the rest got no verdict."""
    if verdict.unmapped:
        ids = sorted(verdict.unmapped)
        count = sum(n for n, _ in verdict.unmapped.values())
        gib = sum((g for _, g in verdict.unmapped.values()), Decimal(0))
        log.warning(
            "cns: %d volume(s), %s GiB, of container cluster id(s) %s no kubernetes "
            "cluster clont reads owns — not judged. map them in inventory.cns_clusters "
            "if they are yours",
            count,
            _gib(gib),
            _names(tuple(ids)),
        )
    if verdict.skipped:
        log.info(
            "cns: no verdict for %s",
            ", ".join(f"{n} ({why})" for why, n in sorted(verdict.skipped.items())),
        )


def snapshot_findings(
    vms: Iterable[Vm],
    region: str,
    storage_rate: Decimal,
    tuning: FinOpsTuning,
    now: datetime,
    labels: dict[str, str] | None = None,
) -> list[Finding]:
    """Old snapshots and pending consolidations, one finding of each kind per vm at most.

    Both are risks before they are savings, so neither is held to the savings floor: a
    two-week-old snapshot of a quiet vm is small and still the thing that breaks a restore.
    A snapshot with no create time is never called old.
    """
    limit = timedelta(days=float(tuning.onprem_snapshot_max_days))
    out: list[Finding] = []
    for vm in vms:
        label = (labels or {}).get(vm.uid, vm.name)
        old = [
            snap
            for snap in vm.snapshots
            if snap.created is not None and now - snap.created > limit
        ]
        if old:
            size = sum((snap.size_gib for snap in old), Decimal(0))
            out.append(
                Finding(
                    kind="old-vm-snapshot",
                    resource_id=label,
                    region=region,
                    summary=_snapshot_summary(vm, old, size, now, tuning),
                    monthly=size * storage_rate,
                    approximate=True,
                    uid=vm.uid,
                )
            )
        if vm.consolidation_needed:
            out.append(
                Finding(
                    kind="consolidation-needed",
                    resource_id=label,
                    region=region,
                    summary=(
                        f"{vm.name} needs its disks consolidated — a snapshot delete left "
                        "delta files in the chain, they keep growing and every write is "
                        "slower for it. run snapshots > consolidate on the vm"
                    ),
                    monthly=Decimal(0),
                    uid=vm.uid,
                )
            )
    return out


def _snapshot_summary(
    vm: Vm, old: list[Snapshot], size: Decimal, now: datetime, tuning: FinOpsTuning
) -> str:
    parts = []
    for snap in old:
        part = f"'{snap.path}' {_age(now - snap.created)}"
        tool = _backup_tool(snap.name)
        if tool:
            part += f" (left behind by {tool}, the backup job did not clean up)"
        parts.append(part)
    held = f"~{_gib_fine(size)} GiB of delta" if size > 0 else "deltas of unknown size"
    return (
        f"{vm.name} has {len(old)} snapshot(s) older than "
        f"{tuning.onprem_snapshot_max_days:g} days: {', '.join(parts)}, holding {held} — "
        "delete them in the snapshot manager so vsphere consolidates the chain. never "
        "remove the delta files by hand, that breaks the disk"
    )


def _backup_tool(name: str) -> str | None:
    upper = name.upper()
    return next((tool for mark, tool in _BACKUP_SNAPSHOTS if mark in upper), None)


def _age(delta: timedelta) -> str:
    days = int(delta.total_seconds() // 86400)
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''}"
    hours = int(delta.total_seconds() // 3600)
    return f"{hours} hour{'s' if hours != 1 else ''}"


def _gib_fine(value: Decimal) -> str:
    # a snapshot delta is often a few gib, where `_gib` rounds to nothing useful
    return f"{float(value):,.1f}" if value < 10 else _gib(value)


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


@dataclass(frozen=True, slots=True)
class _Gap:
    """The storage gap, and how much of it a guest platform owned up to."""

    raw: Decimal
    explained: Decimal = Decimal(0)
    volumes: int = 0
    sources: tuple[str, ...] = ()
    listed: Decimal = Decimal(0)  # what gc named file by file
    rows: int = 0
    skipped: tuple[str, ...] = ()  # datastore names left out, a running vm's layout lags

    @property
    def unexplained(self) -> Decimal:
        return self.raw - self.explained - self.listed

    @property
    def listed_credit(self) -> str:
        if self.listed <= 0:
            return ""
        return (
            f". another {_gib(self.listed)} GiB is listed file by file in {self.rows} "
            "orphaned-vmdk / unregistered-vm / cns-leftover finding(s) and is not "
            "counted here"
        )

    @property
    def skipped_note(self) -> str:
        if not self.skipped:
            return ""
        return (
            f". {_names(self.skipped)} left out: a running vm's layout there is older "
            "than the free-space reading, so its growth would read as nobody's"
        )

    @property
    def credit(self) -> str:
        """The sentence that says the gap is partly somebody's, and whose."""
        if self.explained <= 0:
            return ""
        return (
            f". a further {_gib(self.explained)} GiB of the gap is {self.volumes} "
            f"kubernetes volume(s) no vm holds in {', '.join(self.sources)} — ask the "
            "cluster about those, it accounts for them and this figure does not count "
            "them twice"
        )


def _unaccounted_gib(
    site: SiteInventory,
    tuning: FinOpsTuning,
    guests: Iterable[GuestStorage] = (),
    swept: gc.Sweep | None = None,
    verdict: cns.Verdict | None = None,
) -> _Gap | None:
    """Used datastore space minus what every vm claims, once both thresholds are passed.

    Summed over the site's flat datastore list on purpose: a shared array sits in every
    pool that mounts it, so a per-pool subtraction would charge one cluster for another
    cluster's vms. A negative gap is normal on an array that dedupes or compresses — the
    vms claim more than the disks hold — and is not a finding.

    An array nobody mounts is left out: `unmounted-datastore` already offers its whole
    capacity back, so counting the space on it again would price the same gib twice and
    bury the real leftovers on the live arrays under it.

    **A kubernetes volume no vm holds is part of this gap and is already reported as a
    claim**, so what the clusters own up to (`finops/k8s/datastores.py`) comes off the top:
    without that the same blocks are offered back twice, once as a pvc and once as
    "orphaned vmdks". A cluster that admits to more than the gap holds leaves nothing to
    report rather than a negative one, and the thresholds are applied to what is left —
    the whole point is that the remainder is the part nobody has accounted for.

    Same for what datastore gc listed and the cns leftovers: those GiB are their own
    findings now, so they come off too, never below zero. a cns leftover is detached (not
    in any vm layout) and in no cluster's claim, so it sits in exactly this gap.

    **A running vm's committed can lag the datastore's free space** by many minutes (seen
    live: 25 GiB grown, `summary.storage` 25 minutes behind), and the difference reads as
    space nobody holds. so the datastores of such a vm are left out of the gap — their
    used space and every claim on them, see `_lagging` — and the summary says which.
    """
    dead = {ds.uid for ds in site.unmounted_datastores}
    left_out, vms = _lagging(site, tuning)
    live = [ds for ds in site.datastores if ds.uid not in dead and ds.uid not in left_out]
    skipped = tuple(sorted(ds.name for ds in site.datastores if ds.uid in left_out - dead))
    if skipped:
        log.warning(
            "unaccounted storage: %s left out of the gap, a running vm's layout is older "
            "than the free-space reading — refresh the datastore "
            "(`vim-cmd hostsvc/datastore/refresh`) or wait a few minutes and rerun",
            _names(skipped),
        )
        if not live:
            return None
    used = sum((ds.capacity_gib - ds.free_gib for ds in live), Decimal(0))
    claimed = sum((vm.committed_gib for vm in vms), Decimal(0))
    raw = used - claimed
    owned = [guest for guest in guests if guest.detached_gib > 0]
    explained = min(sum((g.detached_gib for g in owned), Decimal(0)), max(raw, Decimal(0)))
    listed = Decimal(0)
    garbage = [] if swept is None else [g for g in swept.garbage if g.datastore.uid not in left_out]
    leftovers = [] if verdict is None else [
        row for row in verdict.leftovers
        if row.datastore.uid not in left_out and row.datastore.uid not in dead
    ]
    if garbage or leftovers:
        held = sum((g.size_gib for g in garbage), Decimal(0)) + sum(
            (row.size_gib for row in leftovers), Decimal(0)
        )
        listed = min(held, max(raw - explained, Decimal(0)))
    gap = _Gap(
        raw=raw,
        explained=explained,
        volumes=sum(g.volumes for g in owned),
        sources=tuple(sorted(g.source for g in owned)),
        listed=listed,
        rows=len(garbage) + len(leftovers),
        skipped=skipped,
    )
    if used <= 0 or gap.unexplained < Decimal(str(tuning.onprem_unaccounted_min_gib)):
        return None
    if gap.unexplained / used * _HUNDRED < Decimal(str(tuning.onprem_unaccounted_min_pct)):
        return None
    return gap


def _lagging(site: SiteInventory, tuning: FinOpsTuning) -> tuple[set[str], list[Vm]]:
    """datastores to leave out of the gap, and the vms whose claims still count.

    a vm's committed isn't split per datastore, so one that touches a left-out datastore
    takes all of its datastores out with it, until nothing changes
    """
    out = set(gc.stale(site, tuning))
    vms = site.vms()
    if not out:
        return out, vms
    by_name = {ds.name.strip().lower(): ds.uid for ds in site.datastores}
    spans = {vm.uid: gc.uses(vm, by_name) for vm in vms}
    grew = True
    while grew:
        grew = False
        for vm in vms:
            if spans[vm.uid] & out and not spans[vm.uid] <= out:
                out |= spans[vm.uid]
                grew = True
    return out, [vm for vm in vms if not spans[vm.uid] & out]


def _names(names: tuple[str, ...], limit: int = 5) -> str:
    shown = ", ".join(names[:limit])
    return shown if len(names) <= limit else f"{shown} and {len(names) - limit} more"


def _disk_summary(vm: Vm, clones: CloneHistory | None = None) -> str:
    what = "a template" if vm.template else "powered off"
    # vcenter carries no power-off timestamp as a property, only as an event, so the
    # pass cannot say for how long — do not imply it can
    promised = (
        f", {_gib(vm.disk_gib)} GiB promised" if vm.disk_gib > vm.committed_gib else ""
    )
    note = _clone_note(vm, clones) if vm.template else None
    return (
        f"{vm.name} is {what}, still occupying {_gib(vm.committed_gib)} GiB"
        f"{promised}. its ram is back in the pool, its disk is not"
        + (f". {note}" if note else "")
    )


def _clone_note(vm: Vm, clones: CloneHistory | None) -> str | None:
    """What the event log says about this template, or nothing when it can't say."""
    if clones is None:
        return None
    last = clones.last_cloned.get(vm.uid)
    if last is not None:
        return f"last cloned {last:%Y-%m-%d}"
    if vm.uid in clones.too_young or clones.days < CLONE_MIN_WINDOW_DAYS:
        return None
    return f"never cloned in the last {clones.days} days of event history"


def _gib(value: Decimal) -> str:
    return f"{float(value):,.0f}"
