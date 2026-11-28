"""cns-leftover: cns volumes a known cluster holds no pv for, and everything that stays live.

same site as the gc tests: one cluster on one 2000 gib san, 500 used, the vms' layouts
hold 160 of it, so 340 gib is the gap. every cns disk here is 20 gib on disk.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from clont.core.models import Cloud, Period
from clont.finops.base import FinOpsTuning
from clont.finops.guests import GuestStorage
from clont.finops.k8s.datastores import place
from clont.finops.onprem import cns, gc
from clont.finops.onprem.config import OnPremSite
from clont.finops.onprem.waste import OnPremWasteCollector
from clont.providers.k8s.pvs import build_volumes
from clont.providers.onprem.inventory import (
    CnsVolumes,
    DatastoreFile,
    build_site,
    cns_volumes,
)

GIB = 1024**3
NOW = datetime.now(UTC)
OLD = NOW - timedelta(days=30)
PERIOD = Period(start=date(2026, 10, 1), end=date(2026, 10, 4))
DS = "vim.Datastore:ds-1"
URL = "ds:///vmfs/volumes/5f0e-san01/"
CARD = {
    "rate_card": {"hardware_amortization": 7300},
    "weights": {"cpu": 0.5, "ram": 0.3, "storage": 0.2},
}
STORAGE_RATE = 7300 * 0.2 / 2000  # $0.73 per gib-month
INVENTORY = {"endpoint": "vc.lab", "username": "ro", "password": "x"}

CLUSTERS = {
    "vim.ClusterComputeResource:domain-c7": {"name": "prod", "host": ["vim.HostSystem:host-1"]}
}
HOSTS = {
    "vim.HostSystem:host-1": {
        "name": "esx-01",
        "hardware.cpuInfo.numCpuCores": 8,
        "hardware.memorySize": 64 * GIB,
        "runtime.powerState": "poweredOn",
        "datastore": [DS],
        "vm": ["vim.VirtualMachine:vm-10", "vim.VirtualMachine:vm-11"],
    }
}
DATASTORES = {
    DS: {
        "name": "san-01",
        "summary.capacity": 2000 * GIB,
        "summary.freeSpace": 1500 * GIB,
        "summary.uncommitted": 0,
        "summary.url": URL,
        "host": [{"key": "vim.HostSystem:host-1"}],
    }
}


def _vm(name, gib, powered="poweredOn", extra=()):
    folder = f"[san-01] {name}"
    return {
        "name": name,
        "runtime.host": "vim.HostSystem:host-1",
        "runtime.powerState": powered,
        "config.hardware.numCPU": 2,
        "config.hardware.memoryMB": 4096,
        "summary.storage.committed": gib * GIB,
        "summary.storage.uncommitted": 0,
        "config.files.vmPathName": f"{folder}/{name}.vmx",
        "layoutEx.file": [
            {"key": 0, "name": f"{folder}/{name}.vmx", "type": "config", "size": 0},
            {"key": 1, "name": f"{folder}/{name}.vmdk", "type": "diskDescriptor", "size": 0},
            {"key": 2, "name": f"{folder}/{name}-flat.vmdk", "type": "diskExtent",
             "size": gib * GIB},
            *extra,
        ],
    }


def _vms(app_extra=()):
    return {
        "vim.VirtualMachine:vm-10": _vm("app-01", 60, extra=app_extra),
        "vim.VirtualMachine:vm-11": _vm("old-01", 100, powered="poweredOff"),
    }


def _file(path, gib=0, modified=OLD):
    return DatastoreFile(datastore=DS, path=path, size_gib=Decimal(gib), modified=modified)


def _live_files():
    return [
        _file("[san-01] app-01/app-01.vmx"),
        _file("[san-01] app-01/app-01.vmdk"),
        _file("[san-01] app-01/app-01-flat.vmdk", 60),
        _file("[san-01] old-01/old-01.vmx"),
        _file("[san-01] old-01/old-01.vmdk"),
        _file("[san-01] old-01/old-01-flat.vmdk", 100),
    ]


def vol(vid, cluster="k8s-a", *, pv=None, pvc=None, kind="BLOCK", flavor="VANILLA",
        mib=20 * 1024, clusters=None, path=True):
    """a cns volume the way the wire's `_plain` hands it over."""
    entities = []
    if pv:
        entities.append({"entityType": "PERSISTENT_VOLUME", "entityName": pv,
                         "clusterId": cluster})
    if pvc:
        ns, name = pvc.split("/")
        entities.append({"entityType": "PERSISTENT_VOLUME_CLAIM", "entityName": name,
                         "namespace": ns, "clusterId": cluster})
    ids = [cluster] if clusters is None else clusters
    rows = [{"clusterType": "KUBERNETES", "clusterId": c, "vSphereUser": "csi",
             "clusterFlavor": flavor} for c in ids]
    backing = {"capacityInMb": mib}
    if path:
        backing["backingDiskPath"] = f"[san-01] fcd/{vid}.vmdk"
    meta = {"entityMetadata": entities}
    if rows:
        meta |= {"containerCluster": rows[0], "containerClusterArray": rows}
    return {
        "volumeId": {"id": vid},
        "name": pv or vid,
        "volumeType": kind,
        "datastoreUrl": URL,
        "metadata": meta,
        "backingObjectDetails": backing,
    }


def fcd(vid, gib=20, modified=OLD):
    return _file(f"[san-01] fcd/{vid}.vmdk", gib, modified)


def site(volumes, files=(), *, vms=None, cns_read=True):
    built = build_site(CLUSTERS, HOSTS, DATASTORES, _vms() if vms is None else vms)
    found = CnsVolumes(volumes=cns_volumes(volumes), read_at=NOW) if cns_read else None
    return replace(built, files={DS: tuple(_live_files()) + tuple(files)}, cns=found)


def lab(*handles, names=(), read=True):
    return GuestStorage(
        source="lab",
        handles=frozenset(handles) if read else None,
        pv_names=frozenset(names),
    )


class FakeProvider:
    cloud = Cloud.ONPREM
    alias = "dc1"

    def __init__(self, inv, guests=(), mapping=None):
        self.site = OnPremSite(
            inventory=INVENTORY | {"cns_clusters": mapping or {}}, **CARD
        )
        self._inventory = inv
        self._guests = list(guests)

    def inventory(self, *, refresh: bool = False):
        return self._inventory

    def guest_storage(self):
        return self._guests


def advise(inv, guests=(), mapping=None, tuning=None):
    provider = FakeProvider(inv, guests, mapping)
    return OnPremWasteCollector(provider, tuning).recommendations(PERIOD)


def leftovers(recs):
    return [rec for rec in recs if rec.kind == cns.CNS_LEFTOVER]


def warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]


# the wire's shape


def test_a_cns_volume_parses_with_its_clusters_entities_and_disk():
    [v] = cns_volumes([vol("v-1", pv="pvc-1", pvc="db/data")])
    assert v.id == "v-1" and v.name == "pvc-1" and v.capacity_mib == 20480
    assert v.backing == "block" and v.disk_path == "[san-01] fcd/v-1.vmdk"
    assert v.clusters == ("k8s-a",) and v.flavor == "VANILLA" and v.datastore_url == URL
    assert v.named(cns.PV) == ("pvc-1",) and v.named(cns.PVC) == ("db/data",)


def test_the_cluster_array_wins_over_the_single_cluster():
    [v] = cns_volumes([vol("v-1", clusters=["k8s-a", "k8s-b"], kind="FILE", path=False)])
    assert v.clusters == ("k8s-a", "k8s-b") and v.backing == "file" and v.disk_path == ""


def test_a_volume_with_no_id_is_dropped_and_no_metadata_is_no_cluster():
    found = cns_volumes([{"name": "x"}, {"volumeId": {"id": "v-2"}}])
    assert [v.id for v in found] == ["v-2"] and found[0].clusters == ()


# ownership


def test_a_volume_a_pv_points_at_is_live():
    recs = advise(site([vol("v-1")], [fcd("v-1")]), guests=[lab("v-1")])
    assert leftovers(recs) == []


def test_a_learned_cluster_id_is_not_proof_and_gets_no_verdict(caplog):
    # another cluster on the same csi cluster-id would look exactly like this
    inv = site([vol("v-live"), vol("v-gone", pv="pvc-9", pvc="db/data")],
               [fcd("v-live"), fcd("v-gone")])
    assert leftovers(advise(inv, guests=[lab("v-live")])) == []
    [line] = warnings(caplog)
    assert "k8s-a" in line and "not mapped in inventory.cns_clusters" in line
    assert "k8s-a looks like lab's" in line


def test_a_leftover_of_a_mapped_and_learned_cluster_is_one_finding_with_the_story():
    inv = site([vol("v-live"), vol("v-gone", pv="pvc-9", pvc="db/data")],
               [fcd("v-live"), fcd("v-gone")])
    [rec] = leftovers(advise(inv, guests=[lab("v-live")], mapping={"k8s-a": "lab"}))
    assert rec.resource.resource_id == "v-gone" and rec.resource.region == "prod"
    assert rec.approximate
    assert float(rec.estimated_savings.amount) == pytest.approx(20 * STORAGE_RATE, abs=0.01)
    text = rec.summary
    for part in ("v-gone", "pvc-9", "san-01", "k8s-a", "lab", "is likely unused",
                 "mapped in inventory.cns_clusters", "its pvs hold most",
                 "pvc db/data", "no pv there points at it", "same csi cluster-id",
                 "cloud native storage", "never remove the vmdk"):
        assert part in text, part
    assert "delete" not in text


def test_a_second_cluster_on_the_same_id_is_never_a_leftover():
    # lab is mapped, but cns says another vsphere user wrote v-other: a second cluster
    other = vol("v-other")
    for row in other["metadata"]["containerClusterArray"]:
        row["vSphereUser"] = "csi-b"
    inv = site([vol("v-live"), other], [fcd("v-live"), fcd("v-other")])
    assert leftovers(advise(inv, guests=[lab("v-live")], mapping={"k8s-a": "lab"})) == []
    verdict = cns.judge(inv, [lab("v-live")], FinOpsTuning(), NOW, mapping={"k8s-a": "lab"})
    assert any("another vsphere user" in why for why in verdict.skipped)


def test_pvs_of_two_clusters_on_one_id_is_no_verdict():
    inv = site([vol("v-a"), vol("v-b"), vol("v-gone")], [fcd("v-a"), fcd("v-b"), fcd("v-gone")])
    other = replace(lab("v-b"), source="lab2")
    guests = [lab("v-a"), other]
    assert leftovers(advise(inv, guests=guests, mapping={"k8s-a": "lab"})) == []
    verdict = cns.judge(inv, guests, FinOpsTuning(), NOW, mapping={"k8s-a": "lab"})
    assert verdict.skipped == {"pvs of more than one cluster hold volumes of its cluster id": 1}


def test_a_leftover_of_a_config_mapped_cluster_needs_no_live_volume():
    inv = site([vol("v-gone", cluster="k8s-b")], [fcd("v-gone")])
    [rec] = leftovers(advise(inv, guests=[lab()], mapping={"k8s-b": "lab"}))
    assert "mapped in inventory.cns_clusters" in rec.summary


def test_one_matching_pv_does_not_teach_a_cluster_id(caplog):
    # a static pv importing one volume of somebody else's cluster: 1 of 3 is not a share
    inv = site([vol("v-1"), vol("v-2"), vol("v-3")], [fcd("v-1"), fcd("v-2"), fcd("v-3")])
    assert leftovers(advise(inv, guests=[lab("v-1")])) == []
    [line] = warnings(caplog)
    assert "2 volume(s), 40 GiB" in line and "k8s-a" in line


def test_an_unmapped_cluster_is_one_warning_and_no_finding(caplog):
    inv = site([vol("v-1", cluster="k8s-x"), vol("v-2", cluster="k8s-y")],
               [fcd("v-1"), fcd("v-2")])
    caplog.set_level(logging.INFO)
    assert leftovers(advise(inv, guests=[lab()])) == []
    [line] = warnings(caplog)
    assert "2 volume(s), 40 GiB" in line and "k8s-x, k8s-y" in line
    assert "inventory.cns_clusters" in line


def test_a_volume_with_no_cluster_metadata_is_live(caplog):
    inv = site([vol("v-1", clusters=[])], [fcd("v-1")])
    assert leftovers(advise(inv, guests=[lab()], mapping={"k8s-a": "lab"})) == []
    assert warnings(caplog) == []


def test_no_kubernetes_cluster_is_no_verdict_and_no_warning(caplog):
    inv = site([vol("v-1")], [fcd("v-1")])
    assert leftovers(advise(inv, mapping={"k8s-a": "lab"})) == []
    assert not any("cns" in line for line in warnings(caplog))


def test_a_host_agent_has_no_cns_and_nothing_happens():
    inv = site([], cns_read=False)
    assert cns.judge(inv, [lab()], FinOpsTuning(), NOW) is None
    assert leftovers(advise(inv, guests=[lab()])) == []


def test_an_unread_pv_list_is_not_an_empty_one():
    inv = site([vol("v-gone")], [fcd("v-gone")])
    recs = advise(inv, guests=[lab(read=False)], mapping={"k8s-a": "lab"})
    assert leftovers(recs) == []


def test_a_mapping_to_a_cluster_clont_did_not_read_is_no_verdict():
    inv = site([vol("v-gone")], [fcd("v-gone")])
    verdict = cns.judge(inv, [lab()], FinOpsTuning(), NOW, mapping={"k8s-a": "typo"})
    assert verdict.leftovers == ()
    assert verdict.skipped == {"mapped to a kubernetes cluster clont did not read": 1}


def test_the_pv_name_cns_recorded_keeps_it_live():
    # an in-tree pv migrated to csi keeps its path as the handle, its name still matches
    inv = site([vol("v-1", pv="pv-legacy")], [fcd("v-1")])
    recs = advise(inv, guests=[lab(names=["pv-legacy"])], mapping={"k8s-a": "lab"})
    assert leftovers(recs) == []


def test_an_in_tree_path_keeps_it_live():
    inv = site([vol("v-1")], [fcd("v-1")])
    guest = replace(lab(), paths=("[san-01] fcd/v-1.vmdk",))
    assert leftovers(advise(inv, guests=[guest], mapping={"k8s-a": "lab"})) == []


@pytest.mark.parametrize("flavor", ["WORKLOAD", "GUEST_CLUSTER"])
def test_supervisor_and_guest_cluster_volumes_are_not_judged(flavor):
    inv = site([vol("v-1", flavor=flavor)], [fcd("v-1")])
    assert leftovers(advise(inv, guests=[lab()], mapping={"k8s-a": "lab"})) == []


def test_a_file_volume_has_nothing_to_date_it_by():
    inv = site([vol("file:v-1", kind="FILE", path=False)])
    assert leftovers(advise(inv, guests=[lab()], mapping={"k8s-a": "lab"})) == []


def test_a_file_handle_matches_with_or_without_its_prefix():
    inv = site([vol("v-1", kind="FILE", path=False)])
    verdict = cns.judge(inv, [lab("file:V-1")], FinOpsTuning(), NOW, mapping={"k8s-a": "lab"})
    assert verdict.skipped == {} and verdict.leftovers == ()


# the disk side


def test_a_fresh_disk_is_live():
    inv = site([vol("v-1")], [fcd("v-1", modified=NOW - timedelta(days=2))])
    assert leftovers(advise(inv, guests=[lab()], mapping={"k8s-a": "lab"})) == []


def test_a_disk_the_browse_did_not_list_has_no_age_and_is_live():
    inv = site([vol("v-1")])
    assert leftovers(advise(inv, guests=[lab()], mapping={"k8s-a": "lab"})) == []


def test_a_disk_attached_to_a_vm_is_live():
    attached = [{"key": 9, "name": "[san-01] fcd/v-1.vmdk", "type": "diskDescriptor",
                 "size": 0}]
    inv = site([vol("v-1")], [fcd("v-1")], vms=_vms(app_extra=attached))
    assert leftovers(advise(inv, guests=[lab()], mapping={"k8s-a": "lab"})) == []


def test_a_vm_with_no_layout_on_its_datastore_keeps_it_live():
    vms = _vms()
    vms["vim.VirtualMachine:vm-12"] = {
        "name": "blind", "runtime.host": "vim.HostSystem:host-1",
        "runtime.powerState": "poweredOn", "datastore": [DS],
    }
    inv = site([vol("v-1")], [fcd("v-1")], vms=vms)
    assert leftovers(advise(inv, guests=[lab()], mapping={"k8s-a": "lab"})) == []


def test_leftovers_bigger_than_the_gap_are_dropped(caplog):
    inv = site([vol("v-1")], [fcd("v-1", gib=400)])
    assert leftovers(advise(inv, guests=[lab()], mapping={"k8s-a": "lab"})) == []
    assert any("cns parity failed on san-01" in line for line in warnings(caplog))


# parity with the gap


def test_a_leftover_comes_off_unaccounted_storage_and_is_never_priced_twice():
    inv = site([vol("v-1")], [fcd("v-1")])
    recs = advise(inv, guests=[lab()], mapping={"k8s-a": "lab"})
    [rec] = leftovers(recs)
    [gap] = [r for r in recs if r.kind == "unaccounted-storage"]
    # 340 gap - 20 the leftover holds, and gc never listed the fcd disk
    assert [r for r in recs if r.kind == "orphaned-vmdk"] == []
    assert float(gap.estimated_savings.amount) == pytest.approx(320 * STORAGE_RATE, abs=0.01)
    total = float(gap.estimated_savings.amount) + float(rec.estimated_savings.amount)
    assert total == pytest.approx(340 * STORAGE_RATE, abs=0.02)
    assert "cns-leftover" in gap.summary


def test_a_live_volume_the_cluster_claims_is_not_a_leftover_and_its_claim_still_counts():
    inv = site([vol("v-1")], [fcd("v-1")])
    guest = replace(lab("v-1"), detached_gib=Decimal(20), volumes=1)
    recs = advise(inv, guests=[guest])
    assert leftovers(recs) == []
    [gap] = [r for r in recs if r.kind == "unaccounted-storage"]
    assert float(gap.estimated_savings.amount) == pytest.approx(320 * STORAGE_RATE, abs=0.01)


# the k8s side hands over its handles


def _pv(name, handle, driver="csi.vsphere.vmware.com"):
    return {
        "metadata": {"name": name},
        "spec": {"capacity": {"storage": "1Gi"}, "csi": {"driver": driver, "volumeHandle": handle}},
        "status": {"phase": "Bound"},
    }


def test_place_hands_over_csi_handles_and_pv_names():
    pvs = build_volumes([_pv("pv-1", "v-1"), _pv("pv-2", "x", driver="ebs.csi.aws.com")])
    built = build_site(CLUSTERS, HOSTS, DATASTORES, _vms())
    storage = place("lab", [], pvs, [], built).storage
    assert storage.handles == frozenset({"v-1"})
    assert storage.pv_names == frozenset({"pv-1", "pv-2"})


def test_a_failed_pv_read_hands_over_no_handles():
    built = build_site(CLUSTERS, HOSTS, DATASTORES, _vms())
    assert place("lab", [], [], [], built, pvs_read=False).storage.handles is None
    assert place("lab", [], [], [], built, pvs_read=True).storage.handles == frozenset()
    # unknown and empty proves nothing
    assert place("lab", [], [], [], built).storage.handles is None


# the wire, faked


class _Manager:
    def __init__(self, pages, fails=None):
        self.pages = pages
        self.asked: list[tuple[int, int]] = []
        self.fails = fails

    def CnsQueryVolume(self, filter):
        if self.fails is not None:
            raise self.fails
        self.asked.append((filter.cursor.offset, filter.cursor.limit))
        return self.pages[len(self.asked) - 1]


def _wire(monkeypatch, manager, api="VirtualCenter"):
    vim = pytest.importorskip("pyVmomi").vim
    from clont.providers.onprem import vsphere

    session = vsphere.VsphereInventory("vc.lab", "ro", "x")
    session._si, session._vim, session.api_type = object(), vim, api
    monkeypatch.setattr(session, "_cns_manager", lambda _vim: manager)
    return vim, session


def _volume(vim, vid):
    return vim.cns.Volume(
        volumeId=vim.cns.VolumeId(id=vid),
        name=f"pvc-{vid}",
        volumeType="BLOCK",
        datastoreUrl=URL,
        metadata=vim.cns.VolumeMetadata(
            containerCluster=vim.cns.ContainerCluster(
                clusterType="KUBERNETES", clusterId="k8s-a", vSphereUser="csi",
                clusterFlavor="VANILLA",
            ),
            entityMetadata=[
                vim.cns.KubernetesEntityMetadata(
                    entityType="PERSISTENT_VOLUME_CLAIM", entityName="data", namespace="db",
                    clusterId="k8s-a",
                )
            ],
        ),
        backingObjectDetails=vim.cns.BlockBackingDetails(
            capacityInMb=1024, backingDiskId=vid, backingDiskPath=f"[san-01] fcd/{vid}.vmdk"
        ),
    )


def _page(vim, ids, offset, total):
    return SimpleNamespace(
        volumes=[_volume(vim, i) for i in ids],
        cursor=SimpleNamespace(offset=offset, totalRecords=total),
    )


def test_the_wire_pages_until_the_total_and_keeps_every_field(monkeypatch):
    vim = pytest.importorskip("pyVmomi").vim
    from clont.providers.onprem import vsphere

    monkeypatch.setattr(vsphere, "CNS_PAGE", 2)
    manager = _Manager([_page(vim, ["a", "b"], 2, 3), _page(vim, ["c"], 3, 3)])
    _, session = _wire(monkeypatch, manager)
    found = session.cns()
    assert found is not None and [v.id for v in found.volumes] == ["a", "b", "c"]
    assert manager.asked == [(0, 2), (2, 2)] and not session.cns_failed
    v = found.volumes[0]
    assert v.disk_path == "[san-01] fcd/a.vmdk" and v.capacity_mib == 1024
    assert v.clusters == ("k8s-a",) and v.named(cns.PVC) == ("db/data",)


def test_a_cursor_that_does_not_move_is_the_last_page(monkeypatch):
    vim = pytest.importorskip("pyVmomi").vim
    # vcsim answers offset 0 and no total
    manager = _Manager([_page(vim, ["a"], 0, None)])
    _, session = _wire(monkeypatch, manager)
    assert [v.id for v in session.cns().volumes] == ["a"] and len(manager.asked) == 1


def test_a_host_agent_is_not_asked_and_not_warned_about(monkeypatch, caplog):
    manager = _Manager([])
    _, session = _wire(monkeypatch, manager, api="HostAgent")
    assert session.cns() is None and manager.asked == [] and not caplog.records


def test_a_missing_privilege_is_one_warning_naming_it(monkeypatch, caplog):
    vim = pytest.importorskip("pyVmomi").vim
    manager = _Manager([], fails=vim.fault.NoPermission(privilegeId="Cns.Searchable"))
    _, session = _wire(monkeypatch, manager)
    assert session.cns() is None and session.cns_failed
    [line] = warnings(caplog)
    assert "Cns.Searchable" in line


def test_no_cns_service_is_one_warning_and_no_verdict(monkeypatch, caplog):
    manager = _Manager([], fails=ConnectionRefusedError("vsan health is down"))
    _, session = _wire(monkeypatch, manager)
    assert session.cns() is None and session.cns_failed
    [line] = warnings(caplog)
    assert "cns volumes unreadable" in line


# gc's verdict on a datastore holds here too


def test_a_datastore_gc_dropped_on_parity_is_not_judged_again(caplog):
    # a 400 gib orphan breaks gc's bound on san-01: its graph is wrong there, so cns's is too
    inv = site([vol("v-1")], [fcd("v-1"), _file("[san-01] scratch/huge-flat.vmdk", 400)])
    recs = advise(inv, guests=[lab()], mapping={"k8s-a": "lab"})
    assert leftovers(recs) == []
    assert any("datastore gc parity failed on san-01" in line for line in warnings(caplog))
    swept = gc.sweep(inv, FinOpsTuning(), NOW)
    assert swept.unreliable == frozenset({DS})
    verdict = cns.judge(inv, [lab()], FinOpsTuning(), NOW, mapping={"k8s-a": "lab"}, swept=swept)
    assert verdict.skipped == {"its datastore can't be judged": 1}


def test_a_datastore_gc_called_stale_is_skipped_not_a_cns_parity_bug(caplog):
    stale = {DS: {**DATASTORES[DS], "summary.freeSpace": 1900 * GIB}}
    built = build_site(CLUSTERS, HOSTS, stale, _vms())
    inv = replace(
        built,
        files={DS: tuple(_live_files()) + (fcd("v-1"),)},
        cns=CnsVolumes(volumes=cns_volumes([vol("v-1")]), read_at=NOW),
    )
    assert leftovers(advise(inv, guests=[lab()], mapping={"k8s-a": "lab"})) == []
    assert not any("cns parity failed" in line for line in warnings(caplog))


def test_a_volume_a_peer_sites_vm_has_attached_is_live():
    swept = replace(
        gc.sweep(site([vol("v-1")], [fcd("v-1")]), FinOpsTuning(), NOW),
        owned=frozenset({"[san-01] fcd/v-1.vmdk"}),
    )
    inv = site([vol("v-1")], [fcd("v-1")])
    verdict = cns.judge(inv, [lab()], FinOpsTuning(), NOW, mapping={"k8s-a": "lab"}, swept=swept)
    assert verdict.leftovers == ()
    assert verdict.skipped == {"attached to a vm": 1}


def test_a_device_backing_counts_as_attached():
    vms = _vms()
    vms["vim.VirtualMachine:vm-10"]["config.hardware.device"] = [
        {"key": 2001, "backing": {"fileName": "[san-01] fcd/v-1.vmdk"}}
    ]
    inv = site([vol("v-1")], [fcd("v-1")], vms=vms)
    assert leftovers(advise(inv, guests=[lab()], mapping={"k8s-a": "lab"})) == []


def test_the_vsphere_user_parses_off_the_container_cluster():
    [volume] = cns_volumes([vol("v-1")])
    assert volume.users == ("csi",)
