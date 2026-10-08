"""The pvc -> datastore link, and the double count it removes.

Two halves, both tested here because neither is worth anything alone:

* `providers/k8s/pvs.py` — what a PersistentVolume says about whose blocks it holds
* `finops/k8s/datastores.py` — the join onto the site's arrays, and the ledger the on-prem
  storage gap subtracts

The number that matters is at the bottom: `unaccounted-storage` and `unmounted-pvc` used to
offer the same GiB back twice, and the gap now loses exactly what the claims account for.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from clont.core.models import Cloud, CloudResource, Money, Period
from clont.finops.base import FinOpsTuning
from clont.finops.guests import GuestStorage
from clont.finops.k8s.datastores import place
from clont.finops.k8s.mapping import Priced, match
from clont.finops.k8s.volumes import RELEASED, UNMOUNTED, reclaim
from clont.finops.models import CostRecord
from clont.finops.onprem.config import OnPremSite
from clont.finops.onprem.waste import OnPremWasteCollector
from clont.providers.k8s.nodes import Node
from clont.providers.k8s.pods import Pod
from clont.providers.k8s.pvs import (
    EXTERNAL,
    IN_NODE,
    ON_DATASTORE,
    UNKNOWN,
    build_volumes,
)
from clont.providers.k8s.volumes import Claim
from clont.providers.onprem.inventory import Datastore, Pool, SiteInventory, Vm, build_site

GIB = 1024**3
DAY = date(2026, 10, 6)
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
VSPHERE_CSI = "csi.vsphere.vmware.com"
SAN_URL = "ds:///vmfs/volumes/5e8d0f12-aabb/"
MOREF = "vim.VirtualMachine:vm-1"


def pv(
    name: str = "pvc-1",
    *,
    gib: int = 100,
    claim: str = "apps/data",
    phase: str = "Bound",
    source: dict | None = None,
    reclaim_policy: str = "Delete",
    age_days: int = 30,
    affinity: str = "",
) -> dict:
    """One pv as the apiserver's json, with only the keys clont reads."""
    namespace, _, pvc = claim.partition("/")
    spec: dict = {
        "capacity": {"storage": f"{gib}Gi"},
        "storageClassName": "vsphere-sc",
        "persistentVolumeReclaimPolicy": reclaim_policy,
        **(source if source is not None else csi()),
    }
    if claim:
        spec["claimRef"] = {"namespace": namespace, "name": pvc}
    if affinity:
        spec["nodeAffinity"] = {
            "required": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "kubernetes.io/hostname",
                                "operator": "In",
                                "values": [affinity],
                            }
                        ]
                    }
                ]
            }
        }
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": (NOW - timedelta(days=age_days)).isoformat().replace(
                "+00:00", "Z"
            ),
        },
        "spec": spec,
        "status": {"phase": phase},
    }


def csi(driver: str = VSPHERE_CSI, *, url: str = SAN_URL, kind: str = "block") -> dict:
    attrs = {"type": f"vSphere CNS {kind.title()} Volume"}
    if url:
        attrs["datastoreurl"] = url
    return {"csi": {"driver": driver, "volumeHandle": "fcd-1", "volumeAttributes": attrs}}


def node() -> Node:
    return Node(
        name="kube-1",
        uid="uid-1",
        vcpu=Decimal(4),
        ram_gib=Decimal(16),
        allocatable_vcpu=Decimal(4),
        allocatable_ram_gib=Decimal(16),
    )


def datastore(
    uid: str = "vim.Datastore:ds-1",
    name: str = "san-01",
    *,
    url: str | None = SAN_URL,
    capacity: int = 2000,
    free: int = 1500,
) -> Datastore:
    return Datastore(
        uid=uid,
        name=name,
        capacity_gib=Decimal(capacity),
        free_gib=Decimal(free),
        provisioned_gib=Decimal(capacity - free),
        url=url,
    )


def site(*, datastores=None, committed: int = 160) -> SiteInventory:
    """One pool, one array, one node vm claiming `committed` GiB of it."""
    arrays = tuple(datastores if datastores is not None else [datastore()])
    vm = Vm(
        uid=MOREF,
        name="kube-1",
        host="esx-01",
        powered_on=True,
        template=False,
        vcpu=4,
        ram_gib=Decimal(16),
        disk_gib=Decimal(committed),
        committed_gib=Decimal(committed),
    )
    pool = Pool(
        name="prod",
        kind="cluster",
        datacenter="DC0",
        hosts=(),
        datastores=arrays,
        vms=(vm,),
    )
    return SiteInventory(pools=(pool,), datastores=arrays)


def ledger(volumes, *, pods=(), claims=(), inventory=None) -> GuestStorage:
    placed = place(
        "lab", list(claims), build_volumes(volumes), list(pods), inventory or site()
    )
    assert placed.storage is not None
    return placed.storage


# --- what a pv says about itself -------------------------------------------------


def test_a_vsphere_csi_block_volume_is_on_the_datastore_and_attachable():
    volume = build_volumes([pv()])[0]
    assert (volume.kind, volume.attachable) == (ON_DATASTORE, True)
    assert volume.datastore_url == SAN_URL
    assert (volume.gib, volume.claim, volume.handle) == (Decimal(100), "apps/data", "fcd-1")


def test_a_cns_file_volume_is_never_a_vms_disk():
    # exported over nfs off vsan file services: no vm ever attaches it
    volume = build_volumes([pv(source=csi(kind="file"))])[0]
    assert (volume.kind, volume.attachable) == (ON_DATASTORE, False)


def test_an_in_tree_volume_path_names_its_datastore_in_brackets():
    source = {"vsphereVolume": {"volumePath": "[san-01] kubevols/kube-dynamic-1.vmdk"}}
    volume = build_volumes([pv(source=source)])[0]
    assert (volume.kind, volume.datastore, volume.datastore_url) == (ON_DATASTORE, "san-01", "")


def test_a_local_volume_is_inside_the_node_it_is_pinned_to():
    volume = build_volumes([pv(source={"local": {"path": "/mnt/data"}}, affinity="kube-2")])[0]
    assert (volume.kind, volume.node) == (IN_NODE, "kube-2")


def test_an_unknown_csi_driver_is_unknown_and_not_assumed_to_be_on_a_datastore():
    volume = build_volumes([pv(source=csi("ebs.csi.aws.com", url=""))])[0]
    assert volume.kind == UNKNOWN


def test_a_known_local_csi_driver_is_in_the_node():
    volume = build_volumes([pv(source=csi("topolvm.io", url=""))])[0]
    assert volume.kind == IN_NODE


def test_an_nfs_share_is_external_and_holds_no_vms_blocks():
    volume = build_volumes([pv(source={"nfs": {"server": "nas", "path": "/export"}})])[0]
    assert (volume.kind, volume.attachable) == (EXTERNAL, False)


def test_a_released_volume_carries_its_old_claim_and_its_policy():
    volume = build_volumes([pv(phase="Released", reclaim_policy="Retain")])[0]
    assert volume.released and volume.reclaim == "Retain"
    assert volume.claim == "apps/data"  # the claimRef outlives the claim
    assert volume.age_days(NOW) == Decimal(30)


def test_a_volume_with_no_recognised_source_is_unknown_not_a_crash():
    volume = build_volumes([pv(source={})])[0]
    assert (volume.kind, volume.driver) == (UNKNOWN, "")


# --- the ledger the storage gap subtracts ---------------------------------------


def test_a_volume_no_pod_holds_is_the_sites_gap():
    held = ledger([pv()])
    assert (held.detached_gib, held.volumes) == (Decimal(100), 1)
    assert held.attached_gib == Decimal(0)
    assert "100 GiB on datastores no vm holds" in held.line()


def test_a_volume_a_scheduled_pod_holds_is_already_inside_its_node_vm():
    pods = [Pod(namespace="apps", name="web", node="kube-1", claims=("data",))]
    held = ledger([pv()], pods=pods)
    assert (held.detached_gib, held.attached_gib) == (Decimal(0), Decimal(100))


def test_a_pending_pod_does_not_attach_anything():
    # the claim is still not inside any vm, so the gap does hold it — the pvc sweep is the
    # half that treats a pending pod as life
    pods = [Pod(namespace="apps", name="web", node="", claims=("data",))]
    held = ledger([pv()], pods=pods)
    assert held.detached_gib == Decimal(100)


def test_a_mounted_file_volume_is_still_the_sites_space():
    pods = [Pod(namespace="apps", name="web", node="kube-1", claims=("data",))]
    held = ledger([pv(source=csi(kind="file"))], pods=pods)
    assert held.detached_gib == Decimal(100)


def test_a_local_volume_is_never_subtracted_because_its_node_already_pays():
    held = ledger([pv(source={"local": {"path": "/mnt/data"}})])
    assert (held.detached_gib, held.in_node_gib) == (Decimal(0), Decimal(100))


def test_an_unknown_driver_is_reported_and_never_subtracted():
    held = ledger([pv(source=csi("ebs.csi.aws.com", url=""))])
    assert (held.detached_gib, held.unknown_gib) == (Decimal(0), Decimal(100))
    assert "on an unknown driver" in held.line()


def test_another_sites_array_is_not_this_sites_gap():
    held = ledger([pv(source=csi(url="ds:///vmfs/volumes/other/"))])
    assert (held.detached_gib, held.elsewhere_gib) == (Decimal(0), Decimal(100))


def test_a_volume_naming_no_datastore_is_still_on_this_site_somewhere():
    # no `datastoreurl` attribute: the gap is a site total, so "on one of these arrays" is
    # all the subtraction needs
    held = ledger([pv(source=csi(url=""))])
    assert (held.detached_gib, held.unplaced_gib) == (Decimal(100), Decimal(100))
    assert "on no named datastore" in held.line()


def test_the_url_join_ignores_a_trailing_slash():
    arrays = [datastore(url="ds:///vmfs/volumes/5e8d0f12-aabb")]
    placed = place("lab", [], build_volumes([pv()]), [], site(datastores=arrays))
    assert placed.by_claim["apps/data"].datastore == "san-01"


def test_two_arrays_of_one_name_resolve_to_neither():
    arrays = [
        datastore(uid="vim.Datastore:ds-1", url=None),
        datastore(uid="vim.Datastore:ds-2", url=None),
    ]
    source = {"vsphereVolume": {"volumePath": "[san-01] kubevols/x.vmdk"}}
    held = ledger([pv(source=source)], inventory=site(datastores=arrays))
    # it names an array clont cannot resolve, so it is left out rather than guessed at
    assert (held.detached_gib, held.elsewhere_gib) == (Decimal(0), Decimal(100))


def test_the_buckets_add_up_to_every_volume_read():
    volumes = [
        pv("a"),                                                  # detached
        pv("b", claim="apps/held"),                               # attached below
        pv("c", gib=50, source={"local": {"path": "/mnt"}}),      # in a node
        pv("d", gib=10, source=csi("ebs.csi.aws.com", url="")),   # unknown
        pv("e", gib=20, source={"nfs": {"server": "nas", "path": "/x"}}),  # elsewhere
    ]
    pods = [Pod(namespace="apps", name="web", node="kube-1", claims=("held",))]
    held = ledger(volumes, pods=pods)
    total = (
        held.detached_gib + held.attached_gib + held.in_node_gib
        + held.unknown_gib + held.elsewhere_gib
    )
    assert total == Decimal(280)


def test_a_claim_is_placed_on_the_pool_that_mounts_its_array():
    placed = place("lab", [], build_volumes([pv()]), [], site())
    assert placed.by_claim["apps/data"].pool == "DC0/prod"


def test_a_shared_array_names_no_single_pool():
    arrays = (datastore(),)
    pools = tuple(
        Pool(name=name, kind="cluster", datacenter="DC0", hosts=(), datastores=arrays, vms=())
        for name in ("prod", "dev")
    )
    inventory = SiteInventory(pools=pools, datastores=arrays)
    placed = place("lab", [], build_volumes([pv()]), [], inventory)
    # either cluster's card could price it, so the cluster's own mean is the honest rate
    assert placed.by_claim["apps/data"].pool == ""


# --- the rate a claim is priced at ----------------------------------------------


def records(*pools: tuple[str, str]) -> list[CostRecord]:
    """One vm line for the node, plus a pool line per (cluster, $/GiB-month)."""
    out = [
        CostRecord(
            cloud=str(Cloud.ONPREM),
            service="vm",
            period=Period(start=DAY, end=DAY),
            alias="dc1",
            cost=Money(amount=Decimal("24.00"), currency="USD"),
            resource=CloudResource(
                cloud=Cloud.ONPREM, service="vm", resource_id="dc1/kube-1", alias="dc1"
            ),
            dimensions={"cluster": "DC0/prod", "moref": MOREF},
        )
    ]
    out.extend(
        CostRecord(
            cloud=str(Cloud.ONPREM),
            service="headroom",
            period=Period(start=DAY, end=DAY),
            alias="dc1",
            cost=Money(amount=Decimal("5.00"), currency="USD"),
            dimensions={
                "cluster": cluster,
                "weights": "cpu=0.5,ram=0.3,storage=0.2",
                "rate_storage_gib_month": rate,
            },
        )
        for cluster, rate in pools
    )
    return out


def claim(name: str = "data", *, ns: str = "apps", gib: str = "100") -> Claim:
    return Claim(
        namespace=ns,
        name=name,
        gib=Decimal(gib),
        phase="Bound",
        storage_class="vsphere-sc",
        volume="pvc-1",
        created=NOW - timedelta(days=30),
    )


def sweep(claims, volumes, *, pods=None, cost=None, **tune):
    # a pod with no claims keeps the namespace alive without attaching anything, so the
    # per-claim kind is what is under test rather than the namespace rollup
    pods = [Pod(namespace="apps", name="web", node="kube-1")] if pods is None else pods
    target = Priced(kind="vm", uid=MOREF, name="kube-1", alias="dc1", pool="DC0/prod")
    placed = place("lab", list(claims), build_volumes(volumes), list(pods), site())
    return reclaim(
        match("lab", [node()], [target]),
        list(pods),
        [],
        list(claims),
        cost if cost is not None else records(("DC0/prod", "0.08")),
        placements=placed,
        tuning=FinOpsTuning(**tune),
        now=NOW,
    )


def test_a_claim_is_priced_on_the_card_of_the_pool_whose_array_holds_it():
    # the node's own pool is DC0/prod at 0.08; the second card must not move this claim
    cost = records(("DC0/prod", "0.08"), ("DC0/dev", "0.50"))
    report = sweep([claim()], [pv()], cost=cost)
    finding = report.findings[0]
    assert (finding.kind, finding.monthly) == (UNMOUNTED, Decimal("8.00"))


def test_a_claim_with_no_volume_read_falls_back_to_the_cluster_mean():
    report = sweep([claim()], [])
    assert report.findings[0].monthly == Decimal("8.00")


def test_a_released_volume_is_a_finding_of_its_own():
    volumes = [pv(phase="Released", reclaim_policy="Retain", claim="apps/gone")]
    report = sweep([], volumes)
    finding = report.findings[0]
    assert (finding.kind, finding.ref) == (RELEASED, "pvc-1")
    assert finding.monthly == Decimal("8.00")
    assert "the deleted claim apps/gone" in finding.summary
    assert "(Retain)" in finding.summary
    assert "the data is still on the array" in finding.summary
    assert report.released == 1


def test_a_released_volume_is_sized_and_aged_like_a_claim():
    small = [pv(phase="Released", gib=2)]
    young = [pv(phase="Released", age_days=1)]
    assert sweep([], small).findings == ()
    assert sweep([], young).findings == ()


def test_a_bound_volume_is_not_a_released_one():
    assert sweep([], [pv()]).findings == ()


# --- the double count this whole step removes -----------------------------------


CARD = {
    "rate_card": {"hardware_amortization": 7300},
    "weights": {"cpu": 0.5, "ram": 0.3, "storage": 0.2},
}
# the pool: two hosts, one 2000 GiB array at 500 GiB used, vms claiming 160 of it
CLUSTERS = {
    "vim.ClusterComputeResource:domain-c7": {
        "name": "prod",
        "host": ["vim.HostSystem:host-1"],
    }
}
HOSTS = {
    "vim.HostSystem:host-1": {
        "name": "esx-01",
        "hardware.cpuInfo.numCpuCores": 16,
        "hardware.memorySize": 128 * GIB,
        "runtime.powerState": "poweredOn",
        "datastore": ["vim.Datastore:ds-1"],
        "vm": ["vim.VirtualMachine:vm-10"],
    }
}
DATASTORES = {
    "vim.Datastore:ds-1": {
        "name": "san-01",
        "summary.capacity": 2000 * GIB,
        "summary.freeSpace": 1500 * GIB,
        "summary.uncommitted": 0,
        "summary.url": SAN_URL,
    }
}
VMS = {
    "vim.VirtualMachine:vm-10": {
        "name": "kube-1",
        "runtime.host": "vim.HostSystem:host-1",
        "runtime.powerState": "poweredOn",
        "config.hardware.numCPU": 4,
        "config.hardware.memoryMB": 16384,
        "summary.storage.committed": 160 * GIB,
        "summary.storage.uncommitted": 0,
    }
}
STORAGE_RATE = Decimal("0.73")  # 7300 * 0.2 / 2000


class FakeProvider:
    cloud = Cloud.ONPREM
    alias = "dc1"

    def __init__(self, guests=()):
        self.site = OnPremSite(**CARD)
        self._guests = list(guests)

    def inventory(self, *, refresh: bool = False):
        return build_site(CLUSTERS, HOSTS, DATASTORES, VMS)

    def guest_storage(self):
        return [guest() if callable(guest) else guest for guest in self._guests]


def gap(guests=()):
    recs = OnPremWasteCollector(FakeProvider(guests)).recommendations(
        Period(start=DAY, end=DAY)
    )
    return next((rec for rec in recs if rec.kind == "unaccounted-storage"), None)


def test_without_a_cluster_the_whole_gap_is_unexplained():
    finding = gap()
    assert finding is not None
    assert finding.estimated_savings.amount == Decimal(340) * STORAGE_RATE
    assert "kubernetes" not in finding.summary


def test_the_gap_loses_exactly_what_the_claims_account_for():
    held = ledger([pv(gib=200)])
    finding = gap([held])
    assert finding is not None
    # 500 used - 160 claimed by vms = 340, of which 200 is the cluster's detached volume
    assert finding.estimated_savings.amount == Decimal(140) * STORAGE_RATE
    assert "140 GiB of datastore space no vm accounts for" in finding.summary
    assert "a further 200 GiB of the gap is 1 kubernetes volume(s) no vm holds in lab" in (
        finding.summary
    )
    assert "does not count them twice" in finding.summary


def test_a_cluster_holding_the_whole_gap_leaves_nothing_to_report():
    assert gap([ledger([pv(gib=400)])]) is None


def test_the_thresholds_apply_to_what_is_left():
    # 340 - 300 = 40 GiB, under the 100 GiB floor
    assert gap([ledger([pv(gib=300)])]) is None


def test_an_attached_volume_does_not_shrink_the_gap():
    pods = [Pod(namespace="apps", name="web", node="kube-1", claims=("data",))]
    finding = gap([ledger([pv(gib=200)], pods=pods)])
    assert finding is not None
    # those blocks are inside the node vm's committed space, so they are not in the gap
    assert finding.estimated_savings.amount == Decimal(340) * STORAGE_RATE


def test_a_provider_with_no_guests_to_ask_is_not_an_error():
    # every site was in this state before kubernetes was read, and most still are
    class Bare(FakeProvider):
        guest_storage = None

    recs = OnPremWasteCollector(Bare()).recommendations(Period(start=DAY, end=DAY))
    assert any(rec.kind == "unaccounted-storage" for rec in recs)
