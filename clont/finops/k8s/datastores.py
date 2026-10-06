"""The pvc -> datastore link: whose blocks a kubernetes volume is really occupying.

The last piece of the k8s step, and the one that fixes a number in the *other* half of the
product. `finops/onprem/waste.py` reports `unaccounted-storage`: datastore used space minus
what every vm claims. On a vmware cluster running kubernetes a large part of that gap is
not isos or dead vm folders at all — it is CNS volumes nothing has attached, which the
k8s sweep already prices as `unmounted-pvc`. Two findings, one set of blocks, and the
operator is told to reclaim the same GiB twice.

So this module places each volume, and the placement decides the arithmetic:

* **a block volume a scheduled pod holds is attached to that node vm**, and vcenter counts
  an attached disk in the vm's committed storage. Those blocks are already on the invoice
  as part of a vm, so they are not in the gap and nothing is subtracted for them.
* **a block volume nothing holds is detached**, sitting in the datastore's own folder,
  belonging to no vm. That is exactly the gap, and it is what gets subtracted.
* **a file volume is never attached at all** (vsan file services export it over nfs), so it
  is the site's space whether a pod has it or not.
* **a `Released` volume belongs to nothing in either world** — no claim, no pod, no vm. It
  is subtracted *and* reported, because no pvc sweep can find it: the pvc is gone.
* **`in-node` and `unknown` are never subtracted.** A local volume is inside a node vm's
  own disk, which the vm already pays for; an unrecognised driver is a guess, and shrinking
  a real finding on a guess hides waste.

**The join is the datastore url, and it is allowed to miss.** A vsphere csi pv carries
`datastoreurl`, which is the vcenter's own `summary.url`; the in-tree plugin carries
`[ds1] kubevols/x.vmdk`, where the name is all there is. Either may be absent, and that is
not a reason to drop the volume: the gap is a **site** number, summed over every datastore,
so "on this site's iron somewhere" is all the subtraction needs. The resolved datastore buys
something else — the pool whose card prices that array, so a claim is priced on the rate
card of the cluster whose datastore it is actually on rather than the mean of the pools its
nodes sit in.

**A volume that names a datastore this site does not mount is left alone.** Two vcenters
and one cluster is unusual but real, and subtracting another site's blocks here would
shrink a gap this site genuinely has.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from clont.core.logging import get_logger
from clont.finops.guests import GuestStorage
from clont.providers.k8s.pods import Pod
from clont.providers.k8s.pvs import IN_NODE, UNKNOWN, Volume
from clont.providers.k8s.volumes import Claim
from clont.providers.onprem.inventory import Datastore, SiteInventory

log = get_logger("clont.finops.k8s.datastores")

# every volume lands in exactly one of the first five, which is what makes the ledger add
# up to the cluster's volumes; `unplaced` is a slice of `detached`, not a sixth bucket
_BUCKETS = ("detached", "attached", "in_node", "unknown", "elsewhere", "unplaced")


@dataclass(frozen=True, slots=True)
class Placement:
    """One volume, and whose space it sits on."""

    volume: Volume
    datastore: str = ""     # the site's datastore name, when the url or the path matched one
    pool: str = ""          # the pool that mounts it, when exactly one does
    mounted: bool = False   # a scheduled pod holds it, so a node vm has it attached
    counted_in_vm: bool = False  # the hypervisor already bills these blocks inside a vm


@dataclass(frozen=True, slots=True)
class Placements:
    """One cluster's volumes placed on a site's iron."""

    cluster: str
    by_claim: dict[str, Placement] = field(default_factory=dict)
    released: tuple[Volume, ...] = ()
    storage: GuestStorage | None = None

    def pool_of(self, claim: Claim) -> str:
        """The pool whose card should price this claim, or "" when it is not resolved."""
        placed = self.by_claim.get(claim.ref)
        return placed.pool if placed is not None else ""


def place(
    cluster: str,
    claims: Iterable[Claim],
    volumes: Iterable[Volume],
    pods: Iterable[Pod],
    site: SiteInventory,
) -> Placements:
    """Join claims to volumes to datastores, and total up what no vm is holding."""
    index = _Datastores(site)
    # scheduled pods only: a pending pod holds nothing on any node, so the disk it is
    # waiting for is not attached to a vm and the gap does hold it
    attached = {
        f"{pod.namespace}/{claim}" for pod in pods if pod.node for claim in pod.claims
    }
    wanted = {claim.ref for claim in claims}

    by_claim: dict[str, Placement] = {}
    released: list[Volume] = []
    gib = dict.fromkeys(_BUCKETS, Decimal(0))
    counted = 0
    for volume in volumes:
        if volume.released:
            released.append(volume)
        datastore = index.find(volume)
        here = volume.on_datastore and not (datastore is None and index.names(volume))
        if not here:
            gib[_bucket(volume)] += volume.gib
            if volume.claim:
                by_claim[volume.claim] = Placement(volume=volume)
            continue
        mounted = volume.claim in attached
        in_vm = volume.attachable and mounted
        if volume.claim:
            by_claim[volume.claim] = Placement(
                volume=volume,
                datastore=datastore.name if datastore is not None else "",
                pool=index.pool_of(datastore),
                mounted=mounted,
                counted_in_vm=in_vm,
            )
        if in_vm:
            gib["attached"] += volume.gib
            continue
        if volume.gib <= 0:
            continue
        gib["detached"] += volume.gib
        counted += 1
        if datastore is None:
            gib["unplaced"] += volume.gib

    storage = GuestStorage(
        source=cluster,
        detached_gib=gib["detached"],
        volumes=counted,
        attached_gib=gib["attached"],
        in_node_gib=gib["in_node"],
        unplaced_gib=gib["unplaced"],
        unknown_gib=gib["unknown"],
        elsewhere_gib=gib["elsewhere"],
    )
    log.debug("%s", storage.line())
    missing = wanted - set(by_claim)
    if missing:
        # a role that lists claims but not volumes, or a claim bound between the two reads
        log.debug("%s: %d claim(s) with no volume read", cluster, len(missing))
    return Placements(
        cluster=cluster,
        by_claim=by_claim,
        released=tuple(released),
        storage=storage,
    )


def _bucket(volume: Volume) -> str:
    """Which tally a volume that is not on this site's datastores belongs in."""
    if volume.kind == IN_NODE:
        return "in_node"
    return "unknown" if volume.kind == UNKNOWN else "elsewhere"


class _Datastores:
    """A site's arrays, indexed by the two things a pv can name them with."""

    def __init__(self, site: SiteInventory) -> None:
        self._by_url = {_url(ds.url): ds for ds in site.datastores if _url(ds.url)}
        names: dict[str, list[Datastore]] = {}
        for ds in site.datastores:
            names.setdefault(ds.name, []).append(ds)
        # a shown name is not unique, so two arrays of one name resolve to neither —
        # same rule as a bios uuid landing on two vms
        self._by_name = {name: hits[0] for name, hits in names.items() if len(hits) == 1}
        self._pools: dict[str, list[str]] = {}
        for pool in site.pools:
            for ds in pool.datastores:
                self._pools.setdefault(ds.uid, []).append(pool.key)

    def find(self, volume: Volume) -> Datastore | None:
        """The array this volume names, or None when it names none clont can resolve."""
        if volume.datastore_url:
            hit = self._by_url.get(_url(volume.datastore_url))
            if hit is not None:
                return hit
        if volume.datastore:
            return self._by_name.get(volume.datastore)
        return None

    def names(self, volume: Volume) -> bool:
        """Whether the volume names a datastore at all — an unresolved name is foreign."""
        return bool(volume.datastore_url or volume.datastore)

    def pool_of(self, datastore: Datastore | None) -> str:
        """The pool that mounts it, or "" when none does or several do.

        A shared san sits in every pool that mounts it, and picking one of them would price
        a claim off whichever cluster happened to come first — the mean of the cluster's own
        pools is the honest answer there.
        """
        if datastore is None:
            return ""
        pools = self._pools.get(datastore.uid, [])
        return pools[0] if len(pools) == 1 else ""


def _url(value: str | None) -> str:
    """Datastore urls differ only by a trailing slash between the two sides."""
    return (value or "").strip().rstrip("/").lower()
