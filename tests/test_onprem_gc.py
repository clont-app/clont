"""datastore gc: file -> vm edges, the extra roots, and the two garbage kinds.

the site is one cluster on one 2000 gib san, 500 used. app-01 and old-01 own 160 gib of
it per their layouts, so 340 gib is the gap gc can explain at most.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from clont.core.models import Cloud, Period
from clont.finops.base import FinOpsTuning
from clont.finops.guests import GuestStorage
from clont.finops.onprem import gc
from clont.finops.onprem.config import OnPremSite
from clont.finops.onprem.waste import OnPremWasteCollector
from clont.providers.onprem.inventory import DatastoreFile, build_site

GIB = 1024**3
NOW = datetime.now(UTC)
OLD = NOW - timedelta(days=30)
PERIOD = Period(start=date(2026, 10, 1), end=date(2026, 10, 4))
DS = "vim.Datastore:ds-1"
CARD = {
    "rate_card": {"hardware_amortization": 7300},
    "weights": {"cpu": 0.5, "ram": 0.3, "storage": 0.2},
}
STORAGE_RATE = 7300 * 0.2 / 2000  # $0.73 per gib-month

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


def _ds(**extra):
    return {
        DS: {
            "name": "san-01",
            "summary.capacity": 2000 * GIB,
            "summary.freeSpace": 1500 * GIB,
            "summary.uncommitted": 0,
            "host": [{"key": "vim.HostSystem:host-1"}],
            **extra,
        }
    }


def _vm(name, gib, powered="poweredOn", extra_files=()):
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
            {
                "key": 2,
                "name": f"{folder}/{name}-flat.vmdk",
                "type": "diskExtent",
                "size": gib * GIB,
            },
            *extra_files,
        ],
    }


def _vms(**override):
    return {
        "vim.VirtualMachine:vm-10": _vm("app-01", 60),
        "vim.VirtualMachine:vm-11": _vm("old-01", 100, powered="poweredOff"),
        **override,
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


def site(files=(), *, datastores=None, vms=None, hosts=None, browsed=True):
    built = build_site(
        CLUSTERS,
        HOSTS if hosts is None else hosts,
        _ds() if datastores is None else datastores,
        _vms() if vms is None else vms,
    )
    if not browsed:
        return built
    return replace(built, files={DS: tuple(_live_files()) + tuple(files)})


def swept(files=(), tuning=None, keep=(), **kwargs):
    return gc.sweep(site(files, **kwargs), tuning or FinOpsTuning(), NOW, keep=keep)


def paths(result):
    return [(g.kind, g.path) for g in result.garbage]


class FakeProvider:
    cloud = Cloud.ONPREM
    alias = "dc1"

    def __init__(self, inv, guests=(), peers=()):
        self.site = OnPremSite(**CARD)
        self._inventory = inv
        self._guests = list(guests)
        self._peers = list(peers)

    def inventory(self, *, refresh: bool = False):
        return self._inventory

    def guest_storage(self):
        return self._guests

    def peer_sites(self):
        return self._peers


def advise(inv, tuning=None, guests=(), peers=()):
    provider = FakeProvider(inv, guests, peers)
    return OnPremWasteCollector(provider, tuning).recommendations(PERIOD)


def by_kind(recs, kind):
    return [rec for rec in recs if rec.kind == kind]


# edges


def test_every_file_a_vm_lists_is_live():
    result = swept()
    assert result.garbage == ()
    assert result.judged == (DS,)


def test_a_disk_on_two_vms_is_one_file_with_two_edges():
    shared = {"key": 9, "name": "[san-01] shared/data.vmdk", "type": "diskDescriptor"}
    vms = _vms(**{"vim.VirtualMachine:vm-10": _vm("app-01", 60, extra_files=[shared])})
    vms["vim.VirtualMachine:vm-11"] = _vm("old-01", 100, "poweredOff", extra_files=[shared])
    result = swept([_file("[san-01] shared/data.vmdk")], vms=vms)
    assert result.garbage == ()
    # one vm letting go of it leaves the other edge
    vms["vim.VirtualMachine:vm-11"] = _vm("old-01", 100, "poweredOff")
    assert swept([_file("[san-01] shared/data.vmdk")], vms=vms).garbage == ()


def test_the_edge_match_ignores_case_and_spacing():
    result = swept([_file("[SAN-01]  App-01/APP-01-flat.vmdk", 1)])
    assert result.garbage == ()


# orphaned-vmdk


def test_a_disk_nobody_lists_is_one_orphan_descriptor_and_extents_together():
    result = swept(
        [
            _file("[san-01] scratch/big.vmdk"),
            _file("[san-01] scratch/big-flat.vmdk", 40),
            _file("[san-01] scratch/big-ctk.vmdk"),
            _file("[san-01] scratch/big-000001.vmdk"),
            _file("[san-01] scratch/big-000001-sesparse.vmdk", 2),
        ]
    )
    assert paths(result) == [
        ("orphaned-vmdk", "[san-01] scratch/big-000001.vmdk"),
        ("orphaned-vmdk", "[san-01] scratch/big.vmdk"),
    ]
    assert result.garbage[1].size_gib == 40
    assert len(result.garbage[1].files) == 3


def test_an_unlisted_disk_in_a_live_vm_folder_is_still_an_orphan():
    result = swept([_file("[san-01] app-01/app-01_1.vmdk"), _file("[san-01] app-01/app-01_1-flat.vmdk", 5)])
    assert paths(result) == [("orphaned-vmdk", "[san-01] app-01/app-01_1.vmdk")]


def test_an_orphaned_vmdk_is_priced_at_its_pool_and_names_its_path():
    [rec] = by_kind(
        advise(site([_file("[san-01] scratch/big.vmdk"), _file("[san-01] scratch/big-flat.vmdk", 40)])),
        "orphaned-vmdk",
    )
    assert rec.resource.resource_id == "[san-01] scratch/big.vmdk"
    assert rec.resource.region == "prod"
    assert rec.approximate is True
    assert float(rec.estimated_savings.amount) == pytest.approx(40 * STORAGE_RATE, abs=0.01)
    assert "big-flat.vmdk" in rec.summary
    assert "is likely unused" in rec.summary
    assert "no vm clont can see lists it" in rec.summary
    assert "before removing it, check" in rec.summary
    assert "30 days" in rec.summary
    assert "delete" not in rec.summary and "low confidence" not in rec.summary


# unregistered-vm


def _ghost(modified=OLD):
    return [
        _file("[san-01] ghost/ghost.vmx", modified=modified),
        _file("[san-01] ghost/ghost.vmdk", modified=modified),
        _file("[san-01] ghost/ghost-flat.vmdk", 30, modified=modified),
        _file("[san-01] ghost/ghost_1.vmdk", modified=modified),
        _file("[san-01] ghost/ghost_1-flat.vmdk", 10, modified=modified),
    ]


def test_a_folder_with_a_vmx_nobody_points_at_is_one_unregistered_vm():
    result = swept(_ghost())
    # the whole folder, and none of its disks again as orphans
    assert paths(result) == [("unregistered-vm", "[san-01] ghost")]
    assert result.garbage[0].size_gib == 40


def test_an_unregistered_vm_lists_its_files_and_never_the_folder():
    recs = advise(site(_ghost()))
    assert by_kind(recs, "orphaned-vmdk") == []
    [rec] = by_kind(recs, "unregistered-vm")
    assert rec.resource.resource_id == "[san-01] ghost"
    text = rec.summary
    for name in ("ghost.vmx", "ghost.vmdk", "ghost-flat.vmdk", "ghost_1.vmdk", "ghost_1-flat.vmdk"):
        assert name in text, name
    assert "is likely an unregistered vm" in text
    assert "another vcenter that shares san-01" in text
    assert "re-register it if it's needed" in text
    assert "leave the folder itself alone" in text
    assert "delete" not in text and "remove the folder" not in text
    assert float(rec.estimated_savings.amount) == pytest.approx(40 * STORAGE_RATE, abs=0.01)


def test_a_stray_vmx_next_to_anything_live_keeps_the_whole_folder():
    # the vmx may name the loose disk in a file we can't read
    result = swept(
        [_file("[san-01] app-01/app-01-old.vmx"), _file("[san-01] app-01/loose.vmdk", 3)]
    )
    assert result.garbage == ()


def test_one_fresh_file_keeps_an_unregistered_folder():
    rows = _ghost()
    rows[2] = _file("[san-01] ghost/ghost-flat.vmdk", 30, modified=NOW - timedelta(hours=1))
    assert swept(rows).garbage == ()


def test_a_live_disk_in_a_subfolder_keeps_an_unregistered_folder():
    # "delete the folder" would take the subfolder, and app-01's second disk, with it
    extra = {"key": 3, "name": "[san-01] ghost/data/app-01_1.vmdk", "type": "diskDescriptor"}
    vms = _vms(**{"vim.VirtualMachine:vm-10": _vm("app-01", 60, extra_files=[extra])})
    rows = [*_ghost(), _file("[san-01] ghost/data/app-01_1.vmdk", 5)]
    assert swept(rows, vms=vms).garbage == ()


def test_a_fresh_file_in_a_subfolder_keeps_an_unregistered_folder():
    rows = [*_ghost(), _file("[san-01] ghost/new/x.vmdk", 1, modified=NOW - timedelta(hours=1))]
    assert swept(rows).garbage == ()


def test_a_dead_subfolder_does_not_save_its_unregistered_parent():
    rows = [*_ghost(), _file("[san-01] ghost/old/x.vmdk", 1)]
    assert paths(swept(rows)) == [
        ("unregistered-vm", "[san-01] ghost"),
        ("orphaned-vmdk", "[san-01] ghost/old/x.vmdk"),
    ]


def test_a_sibling_folder_with_a_shared_prefix_is_not_a_subfolder():
    rows = [*_ghost(), _file("[san-01] ghost2/x.vmdk", 1, modified=NOW - timedelta(hours=1))]
    assert paths(swept(rows)) == [("unregistered-vm", "[san-01] ghost")]


def test_a_stray_vmx_in_the_datastore_root_is_never_the_whole_root():
    assert swept([_file("[san-01] lost.vmx"), _file("[san-01] lost.vmdk")]).garbage == ()


def test_a_registered_vm_whose_vmx_is_not_in_its_layout_still_owns_it():
    vm = _vm("app-01", 60)
    vm["layoutEx.file"] = [f for f in vm["layoutEx.file"] if f["type"] != "config"]
    assert swept(vms=_vms(**{"vim.VirtualMachine:vm-10": vm})).garbage == ()


# roots


@pytest.mark.parametrize(
    "path",
    [
        "[san-01] fcd/0a1b2c.vmdk",
        "[san-01] .fcd/0a1b2c.vmdk",
        "[san-01] catalog/vclock/x.vmdk",
        "[san-01] .vsan.stats/x.vmdk",
        "[san-01] .sdd.sf/x.vmdk",
        "[san-01] .naa.600a0980/x.vmdk",
        "[san-01] .snapshot/hourly.0/app-01/app-01-flat.vmdk",
        "[san-01] contentlib-3f2e/item/disk.vmdk",
        "[san-01] kubevols/pvc-1.vmdk",
        "[san-01] isos/ubuntu.iso",
        "[san-01] appliances/vcsa.ova",
    ],
)
def test_system_csi_library_and_media_paths_are_roots(path):
    assert swept([_file(path, 5)]).garbage == ()


@pytest.mark.parametrize(
    "pin",
    ["[san-01] golden/", "golden", "golden/", "[SAN-01] golden/base.vmdk", "gold*/", "[san-01]"],
)
def test_the_keep_list_pins_paths(pin):
    rows = [_file("[san-01] golden/base.vmdk"), _file("[san-01] golden/base-flat.vmdk", 20)]
    assert swept(rows, tuning=FinOpsTuning(onprem_gc_keep=(pin,))).garbage == ()


def test_a_keep_pin_on_another_datastore_pins_nothing_here():
    rows = [_file("[san-01] golden/base.vmdk", 20)]
    result = swept(rows, tuning=FinOpsTuning(onprem_gc_keep=("[san-02] golden/",)))
    assert paths(result) == [("orphaned-vmdk", "[san-01] golden/base.vmdk")]


def test_a_clusters_in_tree_volume_path_is_a_root():
    rows = [_file("[san-01] myvols/pvc-7.vmdk", 8)]
    assert len(swept(rows).garbage) == 1
    assert swept(rows, keep=["[san-01] myvols/pvc-7.vmdk"]).garbage == ()


def test_a_fcd_volume_is_left_to_kubernetes_and_never_counted_twice():
    # a detached csi volume: the cluster prices it as unmounted-pvc and owns up to it,
    # gc never lists it, and the gap loses it exactly once
    fcd = [_file("[san-01] fcd/9f8e.vmdk", 100)]
    guest = GuestStorage(source="lab", detached_gib=Decimal(100), volumes=1)
    recs = advise(site(fcd), guests=[guest])
    assert by_kind(recs, "orphaned-vmdk") == []
    [gap] = by_kind(recs, "unaccounted-storage")
    # 340 gap - 100 the cluster holds
    assert float(gap.estimated_savings.amount) == pytest.approx(240 * STORAGE_RATE, abs=0.01)


# min age


def test_a_fresh_file_is_live():
    rows = [_file("[san-01] scratch/new.vmdk", 4, modified=NOW - timedelta(days=2))]
    assert swept(rows).garbage == ()
    assert len(swept(rows, tuning=FinOpsTuning(onprem_gc_min_age_days=1)).garbage) == 1


def test_a_file_of_unknown_age_is_never_old_enough():
    assert swept([_file("[san-01] scratch/x.vmdk", 4, modified=None)]).garbage == ()


def test_the_min_age_defaults_to_a_week():
    assert FinOpsTuning().onprem_gc_min_age_days == 7


# what keeps a whole datastore live


def test_a_datastore_a_host_outside_the_pass_mounts_is_all_live(caplog):
    datastores = _ds(host=[{"key": "vim.HostSystem:host-1"}, {"key": "vim.HostSystem:host-77"}])
    with caplog.at_level(logging.WARNING):
        result = swept([_file("[san-01] scratch/x.vmdk", 4)], datastores=datastores)
    assert result.garbage == ()
    assert result.judged == ()
    assert "san-01" in result.reasons
    assert any("san-01" in r.getMessage() for r in caplog.records)


def test_a_datastore_whose_mount_list_never_came_back_is_all_live():
    datastores = _ds()
    del datastores[DS]["host"]
    assert swept([_file("[san-01] scratch/x.vmdk", 4)], datastores=datastores).garbage == ()


def test_vsan_paths_are_never_judged():
    datastores = _ds(**{"summary.type": "vsan"})
    assert swept([_file("[san-01] scratch/x.vmdk", 4)], datastores=datastores).garbage == ()


def test_a_datastore_the_browse_left_out_is_all_live():
    # timed out, failed or denied: no key in `files`, so nothing on it is judged
    other = {
        "vim.Datastore:ds-2": {
            "name": "san-02",
            "summary.capacity": 100 * GIB,
            "summary.freeSpace": 10 * GIB,
            "host": [{"key": "vim.HostSystem:host-1"}],
        }
    }
    built = site([_file("[san-01] scratch/x.vmdk", 4)], datastores={**_ds(), **other})
    result = gc.sweep(built, FinOpsTuning(), NOW)
    assert result.judged == (DS,)
    assert all(g.datastore.uid == DS for g in result.garbage)


def test_no_browse_at_all_is_no_gc_and_the_old_gap():
    assert gc.sweep(site(browsed=False), FinOpsTuning(), NOW) is None
    [gap] = by_kind(advise(site(browsed=False)), "unaccounted-storage")
    assert "cannot browse a datastore" in gap.summary


def test_a_vm_with_no_layout_and_no_datastore_list_turns_gc_off(caplog):
    vm = _vm("app-01", 60)
    del vm["layoutEx.file"]
    with caplog.at_level(logging.WARNING):
        result = swept([_file("[san-01] scratch/x.vmdk", 4)], vms=_vms(**{"vim.VirtualMachine:vm-10": vm}))
    assert result is None
    assert any("app-01" in r.getMessage() for r in caplog.records)


DS2 = "vim.Datastore:ds-2"


def _two_datastores():
    return {
        **_ds(),
        DS2: {
            "name": "san-02",
            "summary.capacity": 100 * GIB,
            "summary.freeSpace": 50 * GIB,
            "host": [{"key": "vim.HostSystem:host-1"}],
        },
    }


def test_a_vm_with_no_layout_blinds_only_its_own_datastores(caplog):
    vm = _vm("app-01", 60)
    del vm["layoutEx.file"]
    vm["datastore"] = [DS2]
    vm["config.files.vmPathName"] = "[san-02] app-01/app-01.vmx"
    hosts = {"vim.HostSystem:host-1": {**HOSTS["vim.HostSystem:host-1"], "datastore": [DS, DS2]}}
    built = site(vms=_vms(**{"vim.VirtualMachine:vm-10": vm}), datastores=_two_datastores(), hosts=hosts)
    on_01 = [f for f in _live_files() if "app-01" not in f.path]
    built = replace(
        built,
        files={
            DS: (*on_01, _file("[san-01] scratch/x-flat.vmdk", 4)),
            DS2: (_file("[san-02] app-01/app-01-flat.vmdk", 60), _file("[san-02] tmp/y-flat.vmdk", 2)),
        },
    )
    with caplog.at_level(logging.WARNING):
        result = gc.sweep(built, FinOpsTuning(), NOW)
    # san-01 still judged, san-02 all-live
    assert paths(result) == [("orphaned-vmdk", "[san-01] scratch/x.vmdk")]
    assert result.judged == (DS,)
    assert "app-01" in result.reasons["san-02"]
    assert any("san-02" in r.getMessage() for r in caplog.records)


def test_a_blind_vm_never_frees_its_own_vmx_folder():
    # its vmx is on the blind datastore, so the folder can't come back as unregistered
    vm = _vm("app-01", 60)
    del vm["layoutEx.file"]
    vm["datastore"] = [DS]
    result = swept(_ghost(), vms=_vms(**{"vim.VirtualMachine:vm-10": vm}))
    assert result.garbage == ()
    assert result.judged == ()


# small orphans fold into one row


def test_small_orphans_on_one_datastore_are_one_finding():
    # $1 floor at $0.73/gib: 1 gib each is under it, 10 gib is over
    rows = [_file(f"[san-01] junk/d{i}-flat.vmdk", 1) for i in range(7)]
    rows.append(_file("[san-01] scratch/big-flat.vmdk", 10))
    found = by_kind(advise(site(rows)), "orphaned-vmdk")
    assert len(found) == 2
    [fold] = [r for r in found if r.resource.resource_id == "[san-01] small-orphans"]
    assert "7 small disks on san-01" in fold.summary
    assert "and 2 more" in fold.summary
    assert float(fold.estimated_savings.amount) == pytest.approx(7 * STORAGE_RATE, abs=0.01)


def test_one_small_orphan_stays_its_own_row():
    found = by_kind(advise(site([_file("[san-01] junk/d0-flat.vmdk", 1)])), "orphaned-vmdk")
    assert [r.resource.resource_id for r in found] == ["[san-01] junk/d0.vmdk"]


def test_folded_orphans_still_come_off_the_gap():
    rows = [_file(f"[san-01] junk/d{i}-flat.vmdk", 1) for i in range(7)]
    recs = advise(site(rows))
    [gap] = by_kind(recs, "unaccounted-storage")
    assert float(gap.estimated_savings.amount) == pytest.approx(333 * STORAGE_RATE, abs=0.01)


# parity, and the gap that is left


def test_more_garbage_than_the_gap_drops_the_datastores_rows(caplog):
    # 340 gib unclaimed, 400 gib of "garbage": the graph is wrong, not the array
    rows = [_file("[san-01] scratch/huge-flat.vmdk", 400)]
    with caplog.at_level(logging.WARNING):
        result = swept(rows)
    assert result.garbage == ()
    assert result.dropped == ("san-01",)
    assert any("parity failed on san-01" in r.getMessage() for r in caplog.records)
    # and the gap goes back to being the answer
    recs = advise(site(rows))
    assert by_kind(recs, "orphaned-vmdk") == []
    [gap] = by_kind(recs, "unaccounted-storage")
    assert float(gap.estimated_savings.amount) == pytest.approx(340 * STORAGE_RATE, abs=0.01)


def test_a_stale_summary_is_all_live_not_a_parity_bug(caplog):
    # 160 gib in the layouts, the summary still says 100 used: seen on lab1 after a power-on
    stale = _ds(**{"summary.freeSpace": 1900 * GIB})
    rows = [_file("[san-01] scratch/loose-flat.vmdk", 0)]
    with caplog.at_level(logging.WARNING):
        result = swept(rows, datastores=stale)
    assert result.garbage == ()
    assert result.dropped == ()
    assert "free space is stale" in result.reasons["san-01"]
    assert not any("parity failed" in r.getMessage() for r in caplog.records)


# a running vm's layout that lags the free-space reading

FREE_AT = NOW - timedelta(minutes=1)


def _lagging_vms(minutes, powered="poweredOn", vm=None):
    vm = vm or _vm("app-01", 60, powered=powered)
    return _vms(**{"vim.VirtualMachine:vm-10": {**vm, "layoutEx.timestamp": FREE_AT - timedelta(minutes=minutes)}})


def _fresh_ds(**extra):
    return _ds(**{"info.timestamp": FREE_AT, **extra})


def test_a_lagging_running_vm_is_no_phantom_gap_and_blinds_its_datastore(caplog):
    # vcsa-1 on lab1: grew 25 gib, layout 25 min behind. 100 more gib used than claimed
    grown = _fresh_ds(**{"summary.freeSpace": 1400 * GIB})
    built = site([_file("[san-01] scratch/loose-flat.vmdk", 4)], datastores=grown, vms=_lagging_vms(25))
    with caplog.at_level(logging.WARNING):
        result = gc.sweep(built, FinOpsTuning(), NOW)
        recs = advise(built)
    assert result.garbage == ()
    assert result.judged == ()
    assert "layout of app-01 is 25 min older than the free-space reading" in result.reasons["san-01"]
    assert by_kind(recs, "unaccounted-storage") == []
    assert by_kind(recs, "orphaned-vmdk") == []
    assert any(
        "san-01 left out of the gap" in r.getMessage() and "hostsvc/datastore/refresh" in r.getMessage()
        for r in caplog.records
    )


def test_a_lagging_vm_leaves_its_datastores_out_and_the_rest_is_still_priced():
    # app-01 lags on san-02 only, san-01 keeps old-01's 100 gib claim against 500 used
    vm = {**_vm("app-01", 60), "datastore": [DS2], "config.files.vmPathName": "[san-02] app-01/app-01.vmx"}
    vm["layoutEx.file"] = [{**f, "name": f["name"].replace("san-01", "san-02")} for f in vm["layoutEx.file"]]
    datastores = {**_fresh_ds(), DS2: {**_two_datastores()[DS2], "info.timestamp": FREE_AT}}
    hosts = {"vim.HostSystem:host-1": {**HOSTS["vim.HostSystem:host-1"], "datastore": [DS, DS2]}}
    built = site(datastores=datastores, vms=_lagging_vms(25, vm=vm), hosts=hosts, browsed=False)
    [gap] = by_kind(advise(built), "unaccounted-storage")
    # the rate is spread over 2100 gib now
    assert float(gap.estimated_savings.amount) == pytest.approx(400 * 7300 * 0.2 / 2100, abs=0.01)
    assert "san-02 left out" in gap.summary


def test_a_vm_spanning_a_left_out_datastore_takes_its_others_with_it():
    # old-01 has a disk on the lagging san-02 too, its committed can't be split, so both go
    vm = {**_vm("app-01", 60), "datastore": [DS2], "config.files.vmPathName": "[san-02] app-01/app-01.vmx"}
    vm["layoutEx.file"] = [{**f, "name": f["name"].replace("san-01", "san-02")} for f in vm["layoutEx.file"]]
    vms = _lagging_vms(25, vm=vm)
    vms["vim.VirtualMachine:vm-11"]["datastore"] = [DS, DS2]
    datastores = {**_fresh_ds(), DS2: {**_two_datastores()[DS2], "info.timestamp": FREE_AT}}
    hosts = {"vim.HostSystem:host-1": {**HOSTS["vim.HostSystem:host-1"], "datastore": [DS, DS2]}}
    built = site(datastores=datastores, vms=vms, hosts=hosts, browsed=False)
    assert by_kind(advise(built), "unaccounted-storage") == []


def test_the_same_lag_on_a_powered_off_vm_is_not_stale():
    built = site(datastores=_fresh_ds(), vms=_lagging_vms(25, powered="poweredOff"))
    assert gc.stale(built, FinOpsTuning()) == {}
    result = gc.sweep(built, FinOpsTuning(), NOW)
    assert result.judged == (DS,)
    [gap] = by_kind(advise(built), "unaccounted-storage")
    assert "left out" not in gap.summary


def test_a_lag_under_the_threshold_is_not_stale():
    built = site(datastores=_fresh_ds(), vms=_lagging_vms(9))
    assert gc.stale(built, FinOpsTuning()) == {}
    assert gc.sweep(built, FinOpsTuning(), NOW).judged == (DS,)
    # and the knob moves it
    assert DS in gc.stale(built, FinOpsTuning(onprem_layout_max_lag_minutes=5))


def test_a_running_vm_with_no_layout_time_is_an_unknown_lag():
    # the datastore says when it read free space, the vm doesn't: its lag can't be ruled out
    built = site([_file("[san-01] scratch/x.vmdk", 4)], datastores=_fresh_ds(), vms=_vms())
    assert gc.stale(built, FinOpsTuning()) == {DS: [("app-01", None)]}
    result = gc.sweep(built, FinOpsTuning(), NOW)
    assert result.garbage == () and result.judged == ()
    assert "no layout time for running app-01" in result.reasons["san-01"]
    assert "unknown" in result.reasons["san-01"]
    recs = advise(built)
    assert by_kind(recs, "orphaned-vmdk") == []
    assert by_kind(recs, "unaccounted-storage") == []


def test_a_powered_off_vm_with_no_layout_time_is_not_stale():
    vms = _vms(**{"vim.VirtualMachine:vm-10": _vm("app-01", 60, powered="poweredOff")})
    built = site(datastores=_fresh_ds(), vms=vms)
    assert gc.stale(built, FinOpsTuning()) == {}


@pytest.mark.parametrize("which", ["datastore", "both"])
def test_a_datastore_with_no_free_time_is_never_stale(which):
    vms = _vms() if which == "both" else _lagging_vms(25)
    datastores = _ds() if which in ("datastore", "both") else _fresh_ds()
    built = site(datastores=datastores, vms=vms)
    assert gc.stale(built, FinOpsTuning()) == {}
    assert gc.sweep(built, FinOpsTuning(), NOW).judged == (DS,)
    [gap] = by_kind(advise(built), "unaccounted-storage")
    assert float(gap.estimated_savings.amount) == pytest.approx(340 * STORAGE_RATE, abs=0.01)


def test_exactly_the_gap_passes_parity():
    assert len(swept([_file("[san-01] scratch/all-flat.vmdk", 340)]).garbage) == 1


def test_what_gc_lists_comes_off_the_unaccounted_gap():
    rows = [*_ghost(), _file("[san-01] scratch/big-flat.vmdk", 100)]
    recs = advise(site(rows))
    listed = sum(float(r.estimated_savings.amount) for r in recs if r.kind in ("orphaned-vmdk", "unregistered-vm"))
    [gap] = by_kind(recs, "unaccounted-storage")
    # 340 - 40 - 100: no gib priced twice
    assert float(gap.estimated_savings.amount) == pytest.approx(200 * STORAGE_RATE, abs=0.01)
    assert listed + float(gap.estimated_savings.amount) == pytest.approx(340 * STORAGE_RATE, abs=0.02)
    assert "140 GiB is listed file by file in 2" in gap.summary
    assert "logs, isos, other files" in gap.summary
    assert "cannot browse" not in gap.summary


def test_a_gap_gc_explains_in_full_is_no_unaccounted_finding():
    recs = advise(site([_file("[san-01] scratch/all-flat.vmdk", 300)]))
    # 40 gib left, under the 100 gib floor
    assert by_kind(recs, "unaccounted-storage") == []
    assert len(by_kind(recs, "orphaned-vmdk")) == 1


def test_a_cluster_path_handed_over_by_the_guest_is_not_garbage():
    rows = [_file("[san-01] myvols/pvc-7.vmdk", 8)]
    guest = GuestStorage(source="lab", detached_gib=Decimal(8), volumes=1, paths=("[san-01] myvols/pvc-7.vmdk",))
    recs = advise(site(rows), guests=[guest])
    assert by_kind(recs, "orphaned-vmdk") == []


# another vcenter on the same array, and vms this login can't see

URL = "ds:///vmfs/volumes/5f0e-san01/"


def _peer(vms, name="shared-san", alias="dc2", url=URL):
    """a second vcenter mounting the same array under its own name"""
    host = {**HOSTS["vim.HostSystem:host-1"], "name": "esx-b1", "vm": list(vms)}
    ds = {DS: {**_ds()[DS], "name": name, "summary.url": url}}
    return gc.Peer(alias, build_site(CLUSTERS, {"vim.HostSystem:host-1": host}, ds, vms))


def _theirs(name, powered="poweredOff"):
    vm = _vm(name, 30, powered=powered)
    # the peer names the array its own way, the url is the same
    return {
        **vm,
        "config.files.vmPathName": f"[shared-san] {name}/{name}.vmx",
        "layoutEx.file": [
            {**f, "name": f["name"].replace("[san-01]", "[shared-san]")}
            for f in vm["layoutEx.file"]
        ],
    }


def _foreign(name="b-db"):
    return [
        _file(f"[san-01] {name}/{name}.vmx"),
        _file(f"[san-01] {name}/{name}.vmdk"),
        _file(f"[san-01] {name}/{name}-flat.vmdk", 30),
    ]


def _with_url(**extra):
    return _ds(**{"summary.url": URL, **extra})


def test_a_peer_vcenters_powered_off_vm_is_live_on_a_shared_array():
    built = site(_foreign(), datastores=_with_url())
    # alone, it looks exactly like an unregistered vm
    assert paths(gc.sweep(built, FinOpsTuning(), NOW)) == [("unregistered-vm", "[san-01] b-db")]
    peer = _peer({"vim.VirtualMachine:vm-50": _theirs("b-db")})
    result = gc.sweep(built, FinOpsTuning(), NOW, peers=[peer])
    assert result.garbage == ()
    assert "[san-01] b-db/b-db-flat.vmdk" in result.owned


def test_a_peer_on_another_array_keeps_nothing_here():
    built = site(_foreign(), datastores=_with_url())
    peer = _peer({"vim.VirtualMachine:vm-50": _theirs("b-db")}, url="ds:///vmfs/volumes/other/")
    assert paths(gc.sweep(built, FinOpsTuning(), NOW, peers=[peer])) == [
        ("unregistered-vm", "[san-01] b-db")
    ]


def test_a_peer_that_could_not_be_read_makes_every_finding_low_confidence():
    built = site(_foreign(), datastores=_with_url())
    [row] = gc.sweep(built, FinOpsTuning(), NOW, peers=[gc.Peer("dc2", None)]).garbage
    assert row.doubts == ("site dc2 could not be read, its vms could use it",)
    recs = advise(built, peers=[("dc2", None)])
    [rec] = by_kind(recs, "unregistered-vm")
    assert "low confidence: site dc2 could not be read" in rec.summary


def test_the_collector_asks_the_provider_for_its_peers():
    built = site(_foreign(), datastores=_with_url())
    peer = _peer({"vim.VirtualMachine:vm-50": _theirs("b-db")})
    assert by_kind(advise(built, peers=[(peer.alias, peer.site)]), "unregistered-vm") == []


def test_a_datastore_naming_vms_the_login_cant_read_is_all_live():
    # `Datastore.vm` hands back a moref the vm read never returned: a folder outside the role
    datastores = _ds(vm=["vim.VirtualMachine:vm-10", "vim.VirtualMachine:vm-99"])
    built = site(_foreign(), datastores=datastores)
    assert built.datastores[0].hidden_vms == ("vim.VirtualMachine:vm-99",)
    result = gc.sweep(built, FinOpsTuning(), NOW)
    assert result.garbage == () and result.judged == ()
    assert "1 vm(s) with files on it are hidden" in result.reasons["san-01"]
    assert DS in result.unreliable


def test_vms_hidden_on_a_host_make_the_findings_low_confidence():
    hosts = {
        "vim.HostSystem:host-1": {
            **HOSTS["vim.HostSystem:host-1"],
            "vm": [*HOSTS["vim.HostSystem:host-1"]["vm"], "vim.VirtualMachine:vm-99"],
        }
    }
    built = site(_foreign(), hosts=hosts)
    assert built.hidden_vms == ("vim.VirtualMachine:vm-99",)
    [rec] = by_kind(advise(built), "unregistered-vm")
    assert "low confidence: 1 vm(s) on this site are hidden from clont's login" in rec.summary


def test_a_stray_vmx_anywhere_lowers_an_orphans_confidence():
    rows = [
        *_ghost(),
        _file("[san-01] scratch/big.vmdk"),
        _file("[san-01] scratch/big-flat.vmdk", 4),
    ]
    by_path = {g.path: g for g in swept(rows).garbage}
    assert by_path["[san-01] scratch/big.vmdk"].doubts == (
        "an unregistered vmx ([san-01] ghost/ghost.vmx) could still name it",
    )
    assert by_path["[san-01] ghost"].doubts == ()


# device backings: what layoutEx doesn't list


def _cdrom(path):
    return {"key": 3000, "backing": {"fileName": path}}


def test_device_backings_are_read_parents_included():
    vm = {
        **_vm("app-01", 60),
        "config.hardware.device": [
            _cdrom("[san-01] iso/ubuntu.iso"),
            {"key": 2000, "backing": {"fileName": "[san-01] app-01/app-01.vmdk",
                                      "parent": {"fileName": "[san-01] base/base.vmdk"}}},
            {"key": 4000, "backing": {"deviceName": "/dev/cdrom"}},
            {"key": 4001},
        ],
    }
    built = site(vms=_vms(**{"vim.VirtualMachine:vm-10": vm}))
    app = next(v for v in built.vms() if v.name == "app-01")
    assert app.device_files == (
        "[san-01] iso/ubuntu.iso",
        "[san-01] app-01/app-01.vmdk",
        "[san-01] base/base.vmdk",
    )


def test_a_disk_only_a_device_names_is_live():
    vm = {**_vm("app-01", 60), "config.hardware.device": [
        {"key": 2001, "backing": {"fileName": "[san-01] data/extra.vmdk"}}
    ]}
    rows = [_file("[san-01] data/extra.vmdk"), _file("[san-01] data/extra-flat.vmdk", 9)]
    assert swept(rows, vms=_vms(**{"vim.VirtualMachine:vm-10": vm})).garbage == ()


def test_an_iso_a_live_vm_mounts_keeps_an_unregistered_folder():
    # the browse lists no iso, so "the folder" would have taken app-01's cdrom with it
    vm = {**_vm("app-01", 60), "config.hardware.device": [_cdrom("[san-01] ghost/tools.iso")]}
    result = swept(_ghost(), vms=_vms(**{"vim.VirtualMachine:vm-10": vm}))
    assert result.garbage == ()


def test_an_unregistered_vm_is_only_the_files_gc_listed():
    [row] = swept(_ghost()).garbage
    assert sorted(f.path.rsplit("/", 1)[-1] for f in row.files) == [
        "ghost-flat.vmdk", "ghost.vmdk", "ghost.vmx", "ghost_1-flat.vmdk", "ghost_1.vmdk",
    ]


def test_no_gc_finding_tells_anyone_to_delete_anything():
    rows = [
        *_ghost(),
        _file("[san-01] scratch/big.vmdk"),
        _file("[san-01] scratch/big-flat.vmdk", 40),
        _file("[san-01] tiny/a.vmdk"),
        _file("[san-01] tiny/b.vmdk"),
    ]
    recs = [r for r in advise(site(rows)) if r.kind in ("orphaned-vmdk", "unregistered-vm")]
    assert {r.resource.resource_id for r in recs} == {
        "[san-01] ghost", "[san-01] scratch/big.vmdk", "[san-01] small-orphans",
    }
    for rec in recs:
        text = rec.summary.lower()
        assert "likely" in text and "before removing" in text, rec.summary
        assert "delete" not in text and "remove the folder" not in text, rec.summary


def test_a_peer_hiding_vms_on_a_shared_array_lowers_confidence():
    built = site(_foreign("old-x"), datastores=_with_url())
    peer = _peer({"vim.VirtualMachine:vm-50": _theirs("b-db")})
    hidden = replace(peer.site, hidden_vms=("vim.VirtualMachine:vm-77",))
    [row] = gc.sweep(built, FinOpsTuning(), NOW, peers=[gc.Peer("dc2", hidden)]).garbage
    assert row.doubts == ("1 vm(s) on site dc2 are hidden from clont's login, they could use it",)
    elsewhere = _peer({}, url="ds:///vmfs/volumes/other/")
    hidden = replace(elsewhere.site, hidden_vms=("vim.VirtualMachine:vm-77",))
    [row] = gc.sweep(built, FinOpsTuning(), NOW, peers=[gc.Peer("dc2", hidden)]).garbage
    assert row.doubts == ()


def test_a_shared_array_the_peers_login_sees_hidden_vms_on_is_not_judged():
    # peer b hides a powered-off vm owning [shared-san] x/x.vmdk: on our side it's san-01
    built = site(
        [_file("[san-01] x/x.vmdk"), _file("[san-01] x/x-flat.vmdk", 20)],
        datastores=_with_url(),
    )
    assert paths(gc.sweep(built, FinOpsTuning(), NOW)) == [("orphaned-vmdk", "[san-01] x/x.vmdk")]
    peer = _peer({}, alias="b")
    [ds] = peer.site.datastores
    hidden = replace(
        peer.site, datastores=(replace(ds, hidden_vms=("vim.VirtualMachine:vm-77",)),)
    )
    result = gc.sweep(built, FinOpsTuning(), NOW, peers=[gc.Peer("b", hidden)])
    assert result.garbage == () and result.judged == ()
    assert "1 vm(s) of site b with files on it are hidden" in result.reasons["san-01"]
    assert DS in result.unreliable
    recs = advise(built, peers=[("b", hidden)])
    assert by_kind(recs, "orphaned-vmdk") == []
