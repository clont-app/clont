"""reachability gc for datastore files: mark from the vms, sweep what's left

the graph is file -> vm, one edge per entry in a vm's `layoutEx.file`. that single edge
covers base disks, snapshot deltas, vmx, nvram, swap and logs, and a disk attached to two
vms is two edges onto one file. a vm itself is always a root, gc never judges it —
`stopped-vm` and `idle-vm` do.

other roots, all live whatever their edges say:

* anything under a dot dir (`.fcd`, `.vsan*`, `.sdd.sf`, `.naa.*`, `.dvsData`, `.locker`)
  and the `fcd/` / `catalog/` dirs — first class disks, csi owns them, and kubernetes
  prices them as `released-pv` / `unmounted-pvc`
* `kubevols/` and any path a cluster names as its in-tree volume, same reason
* content library dirs (`contentlib-*`), isos, ovas and ovfs — not disks
* `finops.onprem_gc_keep`, the on-prem `clont:keep`

what's left and old enough (`onprem_gc_min_age_days`) is garbage, in two shapes: a `.vmx`
no inventory vm points at is one `unregistered-vm`, with the disks next to it; a disk with
no edge anywhere else is one `orphaned-vmdk`, descriptor and extents together. both are a
list of files, never a folder: the browse only lists `.vmdk` and `.vmx`, so whatever else
sits in the folder (an iso, logs, nvram) was never looked at.

edges come from more than the layout: a device's backing file (a mounted iso, a disk
layoutEx skipped) and the vms of every peer site clont reads, matched onto our datastores
by url, so a datastore two vcenters share keeps the other one's files live.

conservative all the way, a false "garbage" deletes someone's data:

* a datastore not browsed (no grant, timed out, failed) is all-live
* a datastore a host outside this pass mounts is all-live, it may carry another vcenter's
  disks. so is one whose mount list never came back, one that names vms this login can't
  read (a folder outside the role), and vsan / vvol, whose browse paths don't match what
  `layoutEx` names
* a vcenter only lists its own hosts as mounts, so a second vcenter on the same array is
  invisible unless it's a peer. what gc can't rule out goes in `Garbage.doubts` and the
  finding says so: vms hidden from the login, a peer site that couldn't be read, a stray
  vmx that may name a loose disk
* a vm with no `layoutEx` could own any file on the datastores it uses, so those are
  all-live. one that doesn't say which datastores it uses turns gc off for the site
* so is a running vm whose layout is older than the free-space reading of a datastore it
  uses (`onprem_layout_max_lag_minutes`): it has grown since, and its new blocks would
  read as nobody's. a running vm with no layout time there is the same, its lag unknown
* unknown modification time is never old enough
* a folder with a stray vmx and anything live in it is all-live, the vmx may name the rest

the parity self-test closes it: per datastore the garbage can never be more than used
space minus what the vms' layouts hold there. above that it's a gc bug, the datastore's
rows are dropped and logged.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from clont.core.logging import get_logger
from clont.finops.base import FinOpsTuning
from clont.providers.onprem.inventory import Datastore, DatastoreFile, SiteInventory, Vm

log = get_logger("clont.finops.onprem.gc")

ORPHANED_VMDK = "orphaned-vmdk"
UNREGISTERED_VM = "unregistered-vm"

# a disk is a descriptor plus these extents, all one thing to delete
_EXTENTS = ("-flat", "-delta", "-sesparse", "-ctk", "-rdm", "-rdmp")
_ROOT_DIRS = ("fcd", "catalog", "kubevols")
_NOT_DISKS = (".iso", ".ova", ".ovf", ".img", ".flp")
# browse paths on these don't name what layoutEx names, so nothing could ever match
_BLIND_KINDS = ("vsan", "vvol")


@dataclass(frozen=True, slots=True)
class Peer:
    """another site clont reads. its vms may own files on an array we share"""

    alias: str
    site: SiteInventory | None  # none when its pass failed


@dataclass(frozen=True, slots=True)
class Garbage:
    """One finding's worth of files: a disk, or an unregistered vmx and its disks."""

    kind: str
    datastore: Datastore
    path: str  # the descriptor, or the vmx's folder
    files: tuple[DatastoreFile, ...]  # exactly what was judged, nothing else in the folder
    # what gc could not rule out, empty when it's sure
    doubts: tuple[str, ...] = ()

    @property
    def size_gib(self) -> Decimal:
        return sum((f.size_gib for f in self.files), Decimal(0))

    @property
    def modified(self) -> datetime:
        # every file has a time, an unknown one never gets this far
        return max(f.modified for f in self.files if f.modified is not None)


@dataclass(frozen=True, slots=True)
class Sweep:
    """What gc found, and on which datastores it was allowed to look."""

    garbage: tuple[Garbage, ...] = ()
    judged: tuple[str, ...] = ()   # datastore morefs gc swept
    dropped: tuple[str, ...] = ()  # datastore names whose rows failed the parity test
    reasons: dict[str, str] = field(default_factory=dict)  # name -> why not judged
    # datastore morefs gc refused or dropped, cns must not judge them either
    unreliable: frozenset[str] = frozenset()
    # every normalized path some vm, ours or a peer's, points at
    owned: frozenset[str] = frozenset()

    @property
    def size_gib(self) -> Decimal:
        return sum((g.size_gib for g in self.garbage), Decimal(0))


def sweep(
    site: SiteInventory,
    tuning: FinOpsTuning,
    now: datetime,
    keep: Iterable[str] = (),
    peers: Iterable[Peer] = (),
) -> Sweep | None:
    """Every unowned disk and unregistered vmx. None when gc can't run at all.

    `keep` is extra pinned paths on top of `onprem_gc_keep` — a cluster's in-tree volumes.
    `peers` are the other sites clont reads.
    """
    if not site.files:
        return None
    vms = site.vms()
    lost = [vm.name for vm in vms if not vm.files and not vm.datastores]
    if lost:
        log.warning(
            "datastore gc skipped: no layoutEx and no datastore list for %s, "
            "their files could be anywhere",
            _few(lost),
        )
        return None
    # datastore moref -> vms with no layout on it, their files could be any of its files
    by_name = {_key(ds.name): ds.uid for ds in site.datastores}
    blind: dict[str, list[str]] = {}
    for vm in vms:
        if not vm.files:
            # its vmx counts too, in case the datastore list is short
            home = by_name.get(_key(_split(vm.vmx_path)[0])) if vm.vmx_path else None
            for uid in {*vm.datastores, *([home] if home else [])}:
                blind.setdefault(uid, []).append(vm.name)

    peers = list(peers)
    owned = _owned(site, peers)
    doubts = _site_doubts(site, peers)
    strays = _strays(site, owned)
    pins = [*tuning.onprem_gc_keep, *keep]
    fresh = timedelta(days=float(tuning.onprem_gc_min_age_days))

    garbage: list[Garbage] = []
    judged: list[str] = []
    dropped: list[str] = []
    reasons: dict[str, str] = {}
    unreliable: set[str] = set()
    dead = {ds.uid for ds in site.unmounted_datastores}
    claimed = _claimed(site)
    lagging = stale(site, tuning)
    for ds in site.datastores:
        if ds.uid not in site.files:
            continue
        why = _not_judged(ds, dead)
        if not why and ds.uid in blind:
            why = f"no layoutEx for {_few(blind[ds.uid])}"
        if not why and ds.uid in lagging:
            why = lag_reason(lagging[ds.uid])
        if why:
            reasons[ds.name] = why
            unreliable.add(ds.uid)
            log.warning("datastore gc: every file on %s counts as live, %s", ds.name, why)
            continue
        gap = ds.capacity_gib - ds.free_gib - claimed.get(_key(ds.name), Decimal(0))
        if gap < 0:
            # the layouts hold more than the summary calls used: hostd hasn't caught up
            # with a power-on or a new disk yet, so there is no bound to test against
            why = (
                f"its free space is stale, the vms hold {-gap:.1f} GiB more than it "
                "reports used — refresh the datastore and rerun"
            )
            reasons[ds.name] = why
            unreliable.add(ds.uid)
            log.warning("datastore gc: every file on %s counts as live, %s", ds.name, why)
            continue
        found = _sweep_one(ds, site.files[ds.uid], owned, pins, now - fresh, doubts, strays)
        held = sum((g.size_gib for g in found), Decimal(0))
        if held > gap:
            # more garbage than the space nobody claims: the graph is wrong, not the array
            log.warning(
                "datastore gc parity failed on %s: %.1f GiB of garbage, only %.1f GiB "
                "unclaimed — rows dropped, this is a clont bug",
                ds.name,
                held,
                gap,
            )
            dropped.append(ds.name)
            judged.append(ds.uid)
            unreliable.add(ds.uid)
            continue
        judged.append(ds.uid)
        garbage.extend(found)
    return Sweep(
        garbage=tuple(garbage),
        judged=tuple(judged),
        dropped=tuple(dropped),
        reasons=reasons,
        unreliable=frozenset(unreliable),
        owned=frozenset(owned),
    )


def stale(
    site: SiteInventory, tuning: FinOpsTuning
) -> dict[str, list[tuple[str, float | None]]]:
    """datastore moref -> (vm, minutes) for running vms whose layout lags its free space.

    a powered-off vm doesn't grow, an old layout on it is fine. a datastore with no free
    time has nothing to compare against and is never stale. a running vm with no layout
    time on one that has it is: its lag is unknown, minutes is none
    """
    limit = float(tuning.onprem_layout_max_lag_minutes)
    by_uid = {ds.uid: ds for ds in site.datastores}
    by_name = {_key(ds.name): ds.uid for ds in site.datastores}
    out: dict[str, list[tuple[str, float | None]]] = {}
    for vm in site.vms():
        if not vm.powered_on:
            continue
        for uid in sorted(uses(vm, by_name)):
            ds = by_uid.get(uid)
            if ds is None or ds.free_at is None:
                continue
            if vm.layout_at is None:
                out.setdefault(uid, []).append((vm.name, None))
                continue
            lag = (ds.free_at - vm.layout_at).total_seconds() / 60
            if lag > limit:
                out.setdefault(uid, []).append((vm.name, lag))
    return out


def lag_reason(rows: list[tuple[str, float | None]]) -> str:
    known = [(name, lag) for name, lag in rows if lag is not None]
    unknown = [name for name, lag in rows if lag is None]
    parts = []
    if known:
        worst = max(lag for _, lag in known)
        parts.append(
            f"layout of {_few([n for n, _ in known])} is {worst:.0f} min older than the "
            "free-space reading"
        )
    if unknown:
        parts.append(
            f"no layout time for running {_few(unknown)}, so its lag behind the free-space "
            "reading is unknown"
        )
    return (
        "; ".join(parts) + " — refresh the datastore (`vim-cmd hostsvc/datastore/refresh`) "
        "or wait a few minutes and rerun"
    )


def uses(vm: Vm, by_name: dict[str, str]) -> set[str]:
    """datastore morefs a vm has anything on: its list, its layout and its vmx"""
    out = set(vm.datastores)
    paths = [f.path for f in vm.files] + ([vm.vmx_path] if vm.vmx_path else [])
    for path in paths:
        uid = by_name.get(_key(_split(path)[0]))
        if uid:
            out.add(uid)
    return out


def _not_judged(ds: Datastore, dead: set[str]) -> str:
    if ds.uid in dead:
        return "no host mounts it"
    if ds.unseen_hosts:
        return f"mounted by {len(ds.unseen_hosts)} host(s) outside this pass"
    if ds.hidden_vms:
        return f"{len(ds.hidden_vms)} vm(s) with files on it are hidden from this login"
    if not ds.mount_hosts:
        return "its mount list never came back"
    if (ds.kind or "").lower() in _BLIND_KINDS:
        return f"{ds.kind} paths don't match the vm layouts"
    return ""


def _sweep_one(
    ds: Datastore,
    files: Iterable[DatastoreFile],
    owned: set[str],
    pins: list[str],
    cutoff: datetime,
    doubts: tuple[str, ...] = (),
    strays: tuple[str, ...] = (),
) -> list[Garbage]:
    files = list(files)
    by_folder: dict[str, list[DatastoreFile]] = {}
    for f in files:
        by_folder.setdefault(_folder(f.path), []).append(f)
    here = _key(ds.name)

    def live(f: DatastoreFile) -> bool:
        return (
            _norm(f.path) in owned
            or _root(f.path, pins)
            or f.modified is None
            or f.modified > cutoff
        )

    live_dirs = {_folder(f.path).lower() for f in files if live(f)}
    # a live vm's file the browse never lists, an iso say, keeps its folder live too
    live_dirs |= {_folder(path) for path in owned if _split(path)[0] == here}

    def live_below(folder: str) -> bool:
        # deleting the folder takes its subfolders with it
        prefix = folder.lower() + "/"
        return any(d.startswith(prefix) for d in live_dirs)

    out: list[Garbage] = []
    for folder, rows in sorted(by_folder.items()):
        stray = [
            f for f in rows if f.path.lower().endswith(".vmx") and _norm(f.path) not in owned
        ]
        if stray:
            # a vmx nobody registered names its disks in a file we can't read, so either
            # all its listed files are one finding, or none are. never the datastore root
            if folder.endswith("]"):
                continue
            if folder.lower() in live_dirs or live_below(folder):
                continue
            if not any(live(f) for f in rows):
                out.append(Garbage(UNREGISTERED_VM, ds, folder, tuple(rows), doubts))
            continue
        disks: dict[str, list[DatastoreFile]] = {}
        for f in rows:
            if f.path.lower().endswith(".vmdk"):
                disks.setdefault(_descriptor(f.path), []).append(f)
        for descriptor, parts in sorted(disks.items()):
            if not any(live(f) for f in parts):
                extra = (_stray_doubt(strays),) if strays else ()
                out.append(Garbage(ORPHANED_VMDK, ds, descriptor, tuple(parts), doubts + extra))
    return out


def _owned(site: SiteInventory, peers: list[Peer]) -> set[str]:
    """every normalized path a vm points at: layout, vmx, device backings. a peer's paths
    are renamed onto our datastores by url, its datastore names may differ"""
    out = {_norm(p) for vm in site.vms() for p in _vm_paths(vm)}
    ours: dict[str, set[str]] = {}
    for ds in site.datastores:
        if ds.url:
            ours.setdefault(_url(ds.url), set()).add(ds.name)
    for peer in peers:
        if peer.site is None:
            continue
        names: dict[str, set[str]] = {}
        for ds in peer.site.datastores:
            if ds.url and _url(ds.url) in ours:
                names.setdefault(_key(ds.name), set()).update(ours[_url(ds.url)])
        if not names:
            continue
        for vm in peer.site.vms():
            for path in _vm_paths(vm):
                theirs, rest = _split(path)
                for name in names.get(_key(theirs), ()):
                    out.add(_norm(f"[{name}] {rest}"))
    return out


def _vm_paths(vm: Vm) -> list[str]:
    return [
        *(f.path for f in vm.files),
        *([vm.vmx_path] if vm.vmx_path else []),
        *vm.device_files,
    ]


def _site_doubts(site: SiteInventory, peers: list[Peer]) -> tuple[str, ...]:
    """what makes every finding of this pass less than certain"""
    out = []
    if site.hidden_vms:
        out.append(
            f"{len(site.hidden_vms)} vm(s) on this site are hidden from clont's login, "
            "they could use it"
        )
    failed = sorted(peer.alias for peer in peers if peer.site is None)
    if failed:
        out.append(f"site {', '.join(failed)} could not be read, its vms could use it")
    ours = {_url(ds.url) for ds in site.datastores if ds.url}
    for peer in peers:
        if peer.site is None or not peer.site.hidden_vms:
            continue
        # only a peer that shares an array with us can hide a vm that uses it
        if any(ds.url and _url(ds.url) in ours for ds in peer.site.datastores):
            out.append(
                f"{len(peer.site.hidden_vms)} vm(s) on site {peer.alias} are hidden from "
                "clont's login, they could use it"
            )
    return tuple(out)


def _strays(site: SiteInventory, owned: set[str]) -> tuple[str, ...]:
    """every browsed vmx nobody registered, anywhere on the site"""
    return tuple(
        sorted(
            f.path
            for rows in site.files.values()
            for f in rows
            if f.path.lower().endswith(".vmx") and _norm(f.path) not in owned
        )
    )


def _stray_doubt(strays: tuple[str, ...]) -> str:
    return f"an unregistered vmx ({_few(list(strays), 2)}) could still name it"


def _url(value: str) -> str:
    return value.strip().rstrip("/").lower()


def _claimed(site: SiteInventory) -> dict[str, Decimal]:
    """datastore name -> gib the vm layouts hold there, a shared disk counted once."""
    seen: dict[str, Decimal] = {}
    for vm in site.vms():
        for f in vm.files:
            seen[_norm(f.path)] = f.size_gib
    out: dict[str, Decimal] = {}
    for path, gib in seen.items():
        name = _key(path[1 : path.index("]")]) if path.startswith("[") and "]" in path else ""
        out[name] = out.get(name, Decimal(0)) + gib
    return out


def _root(path: str, pins: list[str]) -> bool:
    _, rest = _split(path)
    parts = rest.lower().split("/")
    dirs = parts[:-1]
    if any(d.startswith(".") for d in dirs):
        return True
    if dirs and dirs[0] in _ROOT_DIRS:
        return True
    if any(d.startswith("contentlib-") for d in dirs):
        return True
    if parts[-1].endswith(_NOT_DISKS):
        return True
    return any(_pinned(path, pin) for pin in pins)


def _pinned(path: str, pin: str) -> bool:
    """`[ds] dir/` pins one datastore, `dir/` every one. globs in the path part only."""
    ds, rest = _split(path)
    pin = pin.strip()
    if pin.startswith("["):
        pin_ds, pin_rest = _split(pin)
        if _key(pin_ds) != _key(ds):
            return False
    else:
        pin_rest = pin.lstrip("/")
    rest, pin_rest = rest.lower(), pin_rest.lower().rstrip("/")
    if not pin_rest:
        return True  # the whole datastore
    # fnmatch reads `[` as a class, and the datastore part is already gone
    return (
        fnmatch.fnmatchcase(rest, pin_rest)
        or fnmatch.fnmatchcase(rest, pin_rest + "/*")
    )


def _folder(path: str) -> str:
    ds, rest = _split(path)
    return f"[{ds}] {rest.rsplit('/', 1)[0]}" if "/" in rest else f"[{ds}]"


def _descriptor(path: str) -> str:
    stem = path[: -len(".vmdk")]
    for suffix in _EXTENTS:
        if stem.lower().endswith(suffix):
            return stem[: -len(suffix)] + ".vmdk"
    return path


def _split(path: str) -> tuple[str, str]:
    """`[ds] a/b.vmdk` -> (`ds`, `a/b.vmdk`). no brackets -> ("", path)."""
    text = path.strip()
    if text.startswith("[") and "]" in text:
        end = text.index("]")
        return text[1:end].strip(), text[end + 1 :].strip().lstrip("/")
    return "", text.lstrip("/")


def _norm(path: str) -> str:
    # vmfs doesn't care about case, and a looser match only ever keeps more files live
    ds, rest = _split(path)
    return f"[{_key(ds)}] {rest.lower()}"


def _key(name: str) -> str:
    return name.strip().lower()


def _few(names: list[str], limit: int = 5) -> str:
    shown = ", ".join(sorted(names)[:limit])
    return shown if len(names) <= limit else f"{shown} and {len(names) - limit} more"
