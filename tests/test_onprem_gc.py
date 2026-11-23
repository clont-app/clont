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

    def __init__(self, inv, guests=()):
        self.site = OnPremSite(**CARD)
        self._inventory = inv
        self._guests = list(guests)

    def inventory(self, *, refresh: bool = False):
        return self._inventory

    def guest_storage(self):
        return self._guests


def advise(inv, tuning=None, guests=()):
    recs = OnPremWasteCollector(FakeProvider(inv, guests), tuning).recommendations(PERIOD)
    return recs


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
    assert "no vm lists" in rec.summary
    assert "30 days" in rec.summary


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


def test_an_unregistered_vm_says_re_register_or_delete():
    recs = advise(site(_ghost()))
    assert by_kind(recs, "orphaned-vmdk") == []
    [rec] = by_kind(recs, "unregistered-vm")
    assert rec.resource.resource_id == "[san-01] ghost"
    assert "ghost.vmx" in rec.summary
    assert "re-register or delete" in rec.summary
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


def test_a_vm_with_no_layout_turns_gc_off(caplog):
    vm = _vm("app-01", 60)
    del vm["layoutEx.file"]
    with caplog.at_level(logging.WARNING):
        result = swept([_file("[san-01] scratch/x.vmdk", 4)], vms=_vms(**{"vim.VirtualMachine:vm-10": vm}))
    assert result is None
    assert any("app-01" in r.getMessage() for r in caplog.records)


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
