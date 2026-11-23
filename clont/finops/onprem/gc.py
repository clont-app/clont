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

what's left and old enough (`onprem_gc_min_age_days`) is garbage, in two shapes: a folder
whose `.vmx` no inventory vm points at is one `unregistered-vm`, the whole folder; a disk
with no edge anywhere else is one `orphaned-vmdk`, descriptor and extents together.

conservative all the way, a false "garbage" deletes someone's data:

* a datastore not browsed (no grant, timed out, failed) is all-live
* a datastore a host outside this pass mounts is all-live, it may carry another vcenter's
  disks. so is one whose mount list never came back, and vsan / vvol, whose browse paths
  don't match what `layoutEx` names
* a vm with no `layoutEx` could own any file, so gc doesn't run at all
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
from clont.providers.onprem.inventory import Datastore, DatastoreFile, SiteInventory

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
class Garbage:
    """One finding's worth of files: a disk, or a whole unregistered vm folder."""

    kind: str
    datastore: Datastore
    path: str  # the descriptor, or the folder
    files: tuple[DatastoreFile, ...]

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

    @property
    def size_gib(self) -> Decimal:
        return sum((g.size_gib for g in self.garbage), Decimal(0))


def sweep(
    site: SiteInventory,
    tuning: FinOpsTuning,
    now: datetime,
    keep: Iterable[str] = (),
) -> Sweep | None:
    """Every unowned disk and unregistered vm folder. None when gc can't run at all.

    `keep` is extra pinned paths on top of `onprem_gc_keep` — a cluster's in-tree volumes.
    """
    if not site.files:
        return None
    vms = site.vms()
    blind = [vm.name for vm in vms if not vm.files]
    if blind:
        log.warning(
            "datastore gc skipped: no layoutEx for %s, their files could be anywhere",
            _few(blind),
        )
        return None

    owned = {_norm(f.path) for vm in vms for f in vm.files}
    owned |= {_norm(vm.vmx_path) for vm in vms if vm.vmx_path}
    pins = [*tuning.onprem_gc_keep, *keep]
    fresh = timedelta(days=float(tuning.onprem_gc_min_age_days))

    garbage: list[Garbage] = []
    judged: list[str] = []
    dropped: list[str] = []
    reasons: dict[str, str] = {}
    dead = {ds.uid for ds in site.unmounted_datastores}
    claimed = _claimed(site)
    for ds in site.datastores:
        if ds.uid not in site.files:
            continue
        why = _not_judged(ds, dead)
        if why:
            reasons[ds.name] = why
            log.warning("datastore gc: every file on %s counts as live, %s", ds.name, why)
            continue
        found = _sweep_one(ds, site.files[ds.uid], owned, pins, now - fresh)
        held = sum((g.size_gib for g in found), Decimal(0))
        gap = ds.capacity_gib - ds.free_gib - claimed.get(_key(ds.name), Decimal(0))
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
            continue
        judged.append(ds.uid)
        garbage.extend(found)
    return Sweep(
        garbage=tuple(garbage),
        judged=tuple(judged),
        dropped=tuple(dropped),
        reasons=reasons,
    )


def _not_judged(ds: Datastore, dead: set[str]) -> str:
    if ds.uid in dead:
        return "no host mounts it"
    if ds.unseen_hosts:
        return f"mounted by {len(ds.unseen_hosts)} host(s) outside this pass"
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
) -> list[Garbage]:
    files = list(files)
    by_folder: dict[str, list[DatastoreFile]] = {}
    for f in files:
        by_folder.setdefault(_folder(f.path), []).append(f)

    def live(f: DatastoreFile) -> bool:
        return (
            _norm(f.path) in owned
            or _root(f.path, pins)
            or f.modified is None
            or f.modified > cutoff
        )

    live_dirs = {_folder(f.path).lower() for f in files if live(f)}

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
            # the whole folder goes as one, or none of it does. never the datastore root
            if folder.endswith("]"):
                continue
            if not any(live(f) for f in rows) and not live_below(folder):
                out.append(Garbage(UNREGISTERED_VM, ds, folder, tuple(rows)))
            continue
        disks: dict[str, list[DatastoreFile]] = {}
        for f in rows:
            if f.path.lower().endswith(".vmdk"):
                disks.setdefault(_descriptor(f.path), []).append(f)
        for descriptor, parts in sorted(disks.items()):
            if not any(live(f) for f in parts):
                out.append(Garbage(ORPHANED_VMDK, ds, descriptor, tuple(parts)))
    return out


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
