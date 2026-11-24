"""cns volumes no pv owns any more: the on-prem twin of `eks-leftover`

vsphere csi creates a container volume in vcenter's cns for every pv, a first class disk
under `fcd/`. gc treats `fcd/` as a root and kubernetes prices what its pvs hold
(`released-pv`, `unmounted-pvc`), so a volume whose pv is gone is priced by nobody: a
deleted cluster, a `Retain` pv someone deleted, a provisioning that failed half-way. cns
still records it, with the container cluster it came from and the pv/pvc names it had.

the join is the csi `volumeHandle`, which *is* the cns volume id. a cluster id belongs to
a known cluster when:

* `inventory.cns_clusters` maps it to a `kubernetes:` name, or
* clont learns it: the cluster's own pvs point at at least half of that id's volumes.
  only single-cluster volumes teach anything, a shared one names its other owners too

conservative all the way, a false "garbage" deletes someone's data. live, no verdict:

* any pv of any read cluster points at it, by handle, by in-tree path or by the pv name
  cns recorded
* a cluster id nobody maps or learns, a volume with no cluster at all, a supervisor or
  guest cluster flavor (they pass volumes around by pvc name, a handle never matches)
* the owning cluster's pv list wasn't read (a failed read is not an empty one)
* a file volume, or a block one with no disk path: nothing to date or check it by
* attached to a vm (its disk is in a layout), or a vm with no layout could hold it
* its disk wasn't in the browse, so its age is unknown, or it was written inside
  `onprem_gc_min_age_days`
* its datastore is one gc wouldn't judge (mounted outside the pass, vsan/vvol, stale
  layout), or the leftovers there would be more than the space nobody claims
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from clont.core.logging import get_logger
from clont.finops.base import FinOpsTuning
from clont.finops.guests import GuestStorage
from clont.finops.onprem import gc
from clont.providers.onprem.inventory import (
    BLOCK,
    VANILLA,
    CnsVolume,
    Datastore,
    SiteInventory,
)

log = get_logger("clont.finops.onprem.cns")

CNS_LEFTOVER = "cns-leftover"
PV = "PERSISTENT_VOLUME"
PVC = "PERSISTENT_VOLUME_CLAIM"
# a cluster's pvs must point at this share of a cluster id's volumes before clont calls
# the id theirs. one static pv importing a foreign volume teaches nothing
LEARN_MIN_SHARE = Decimal("0.5")


@dataclass(frozen=True, slots=True)
class Leftover:
    """One cns volume whose cluster is known, read, and holds no pv for it."""

    volume: CnsVolume
    cluster: str                 # the cns container cluster id
    owners: tuple[str, ...]      # the clont cluster names it was checked against
    datastore: Datastore
    size_gib: Decimal            # on disk, off the browse
    modified: datetime
    learned: bool = True         # false when only the config mapped the cluster


@dataclass(frozen=True, slots=True)
class Verdict:
    leftovers: tuple[Leftover, ...] = ()
    # cluster id -> (volumes, gib) of ids no known cluster owns
    unmapped: dict[str, tuple[int, Decimal]] = field(default_factory=dict)
    # why other candidates got no verdict, one line each, for the log
    skipped: dict[str, int] = field(default_factory=dict)

    @property
    def size_gib(self) -> Decimal:
        return sum((row.size_gib for row in self.leftovers), Decimal(0))


def judge(
    site: SiteInventory,
    guests: Iterable[GuestStorage],
    tuning: FinOpsTuning,
    now: datetime,
    *,
    mapping: dict[str, str] | None = None,
    swept: gc.Sweep | None = None,
) -> Verdict | None:
    """Every cns volume a known cluster dropped. None when there is nothing to judge with.

    `mapping` is `inventory.cns_clusters`. `swept` is gc's result, so the per-datastore
    bound counts its garbage too.
    """
    guests = list(guests)
    if site.cns is None or not guests:
        return None
    volumes = site.cns.volumes
    by_name = {g.source: g for g in guests}
    index = [_Index(g) for g in guests]
    owners = _owners(volumes, index, mapping or {})

    skipped: dict[str, int] = {}
    unmapped: dict[str, tuple[int, Decimal]] = {}
    candidates: list[tuple[CnsVolume, str, tuple[str, ...]]] = []

    def skip(why: str) -> None:
        skipped[why] = skipped.get(why, 0) + 1

    for volume in volumes:
        if any(i.holds(volume) for i in index):
            continue
        if volume.flavor not in ("", VANILLA):
            skip(f"{volume.flavor.lower()} cluster flavor")
            continue
        if not volume.clusters:
            skip("no container cluster recorded")
            continue
        loose = [c for c in volume.clusters if c not in owners]
        if loose:
            for cid in loose:
                count, gib = unmapped.get(cid, (0, Decimal(0)))
                unmapped[cid] = (count + 1, gib + volume.capacity_gib)
            continue
        names = tuple(sorted({n for c in volume.clusters for n in owners[c][0]}))
        if any(n not in by_name for n in names):
            skip("mapped to a kubernetes cluster clont did not read")
            continue
        if any(by_name[n].handles is None for n in names):
            skip("its cluster's pv list was not read")
            continue
        candidates.append((volume, volume.clusters[0], names))

    leftovers = _place(site, candidates, owners, tuning, now, swept, skip)
    return Verdict(leftovers=tuple(leftovers), unmapped=unmapped, skipped=skipped)


def _owners(
    volumes: Iterable[CnsVolume], index: list[_Index], mapping: dict[str, str]
) -> dict[str, tuple[set[str], bool]]:
    """cluster id -> (clont cluster names, learned?). config and learning both count."""
    total: dict[str, int] = {}
    hits: dict[str, dict[str, int]] = {}
    for volume in volumes:
        if len(volume.clusters) != 1 or volume.flavor not in ("", VANILLA):
            continue
        cid = volume.clusters[0]
        total[cid] = total.get(cid, 0) + 1
        for i in index:
            if i.by_handle(volume):
                row = hits.setdefault(cid, {})
                row[i.source] = row.get(i.source, 0) + 1
    out: dict[str, tuple[set[str], bool]] = {}
    for cid, by_guest in hits.items():
        learned = {n for n, k in by_guest.items() if Decimal(k) >= LEARN_MIN_SHARE * total[cid]}
        if learned:
            out[cid] = (learned, True)
    for cid, name in mapping.items():
        names, learned = out.get(cid, (set(), False))
        out[cid] = (names | {name}, learned)
    return out


def _place(
    site: SiteInventory,
    candidates: list[tuple[CnsVolume, str, tuple[str, ...]]],
    owners: dict[str, tuple[set[str], bool]],
    tuning: FinOpsTuning,
    now: datetime,
    swept: gc.Sweep | None,
    skip: Callable[[str], None],
) -> list[Leftover]:
    """The disk-side checks: attached, dated, old enough, and inside the datastore's gap."""
    if not candidates:
        return []
    by_url = {_url(ds.url): ds for ds in site.datastores if ds.url}
    by_ds_name = {gc._key(ds.name): ds for ds in site.datastores}
    vms = site.vms()
    attached = {gc._norm(gc._descriptor(f.path)) for vm in vms for f in vm.files}
    # a vm with no layout could have any of these attached
    blind_all = any(not vm.files and not vm.datastores for vm in vms)
    blind = {uid for vm in vms if not vm.files for uid in vm.datastores}
    dead = {ds.uid for ds in site.unmounted_datastores}
    lagging = gc.stale(site, tuning)
    cutoff = now - timedelta(days=float(tuning.onprem_gc_min_age_days))

    found: list[Leftover] = []
    for volume, cid, names in candidates:
        if volume.backing != BLOCK or not volume.disk_path:
            skip("a file volume or no disk path, nothing to date it by")
            continue
        disk = gc._norm(volume.disk_path)
        ds = by_url.get(_url(volume.datastore_url)) or by_ds_name.get(
            gc._key(gc._split(volume.disk_path)[0])
        )
        if ds is None:
            skip("its datastore is not in this pass")
            continue
        if disk in attached:
            skip("attached to a vm")
            continue
        if blind_all or ds.uid in blind or ds.uid in lagging or gc._not_judged(ds, dead):
            skip("its datastore can't be judged")
            continue
        files = [f for f in site.files.get(ds.uid, ()) if gc._norm(gc._descriptor(f.path)) == disk]
        if not files or any(f.modified is None for f in files):
            skip("its disk was not in the browse, age unknown")
            continue
        modified = max(f.modified for f in files if f.modified is not None)
        if modified > cutoff:
            skip("written inside the min age")
            continue
        size = sum((f.size_gib for f in files), Decimal(0))
        found.append(
            Leftover(
                volume=volume,
                cluster=cid,
                owners=names,
                datastore=ds,
                size_gib=size,
                modified=modified,
                learned=all(owners[c][1] for c in volume.clusters),
            )
        )
    return _bounded(site, found, swept, skip)


def _bounded(
    site: SiteInventory,
    found: list[Leftover],
    swept: gc.Sweep | None,
    skip: Callable[[str], None],
) -> list[Leftover]:
    """gc's parity test again: per datastore never more than the space nobody claims."""
    claimed = gc._claimed(site)
    garbage: dict[str, Decimal] = {}
    for row in swept.garbage if swept is not None else ():
        garbage[row.datastore.uid] = garbage.get(row.datastore.uid, Decimal(0)) + row.size_gib
    by_ds: dict[str, list[Leftover]] = {}
    for row in found:
        by_ds.setdefault(row.datastore.uid, []).append(row)
    out: list[Leftover] = []
    for uid, rows in by_ds.items():
        ds = rows[0].datastore
        gap = (
            ds.capacity_gib
            - ds.free_gib
            - claimed.get(gc._key(ds.name), Decimal(0))
            - garbage.get(uid, Decimal(0))
        )
        held = sum((r.size_gib for r in rows), Decimal(0))
        if held > gap:
            log.warning(
                "cns parity failed on %s: %.1f GiB of leftovers, only %.1f GiB unclaimed — "
                "rows dropped, this is a clont bug",
                ds.name,
                held,
                gap,
            )
            for _ in rows:
                skip("parity failed on its datastore")
            continue
        out.extend(rows)
    return out


class _Index:
    """One cluster's pvs, keyed the three ways a cns volume can be matched."""

    def __init__(self, guest: GuestStorage) -> None:
        self.source = guest.source
        self._handles = (
            {_key(h) for h in guest.handles} if guest.handles is not None else set()
        )
        self._paths = {gc._norm(p) for p in guest.paths}
        self._names = guest.pv_names

    def by_handle(self, volume: CnsVolume) -> bool:
        return _key(volume.id) in self._handles

    def holds(self, volume: CnsVolume) -> bool:
        """Any pv here points at the volume. loose on purpose, it only keeps more live."""
        if self.by_handle(volume):
            return True
        if volume.disk_path and gc._norm(volume.disk_path) in self._paths:
            return True
        return any(name in self._names for name in volume.named(PV))


def _key(handle: str) -> str:
    # a file volume's handle is `file:<id>`, its cns id may or may not carry the prefix
    text = handle.strip().lower()
    return text.removeprefix("file:")


def _url(value: str | None) -> str:
    return (value or "").strip().rstrip("/").lower()
