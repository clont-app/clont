"""The inventory transform: pools, capacity, and what a vm is charged for.

The wire itself is `test_vsphere_vcsim.py` (opt-in) and test layer 1 in the cft functests. What is here is the pure
half — property dicts in, pools out — plus the handoff into `allocate()`, because the
whole point of the transform is to produce that payload.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from clont.core.errors import ConfigError
from clont.finops.onprem.rates import allocate
from clont.providers.onprem.inventory import build_site

GIB = 1024**3

DATACENTERS = {"vim.Datacenter:datacenter-2": {"name": "DC0"}}
# anything with a parent: the host folder, plus the compute resource a standalone host
# hangs off. both resolve to the same datacenter
ANCESTRY = {
    "vim.Folder:group-h4": {"name": "host", "parent": "vim.Datacenter:datacenter-2"},
    "vim.ComputeResource:domain-s9": {
        "name": "esx-standalone",
        "parent": "vim.Folder:group-h4",
    },
}

CLUSTERS = {
    "vim.ClusterComputeResource:domain-c7": {
        "name": "prod-gen11",
        "parent": "vim.Folder:group-h4",
        "host": ["vim.HostSystem:host-1", "vim.HostSystem:host-2"],
    }
}

HOSTS = {
    "vim.HostSystem:host-1": {
        "name": "esx-01",
        "parent": "vim.ClusterComputeResource:domain-c7",
        "hardware.cpuInfo.numCpuCores": 16,
        "hardware.cpuInfo.numCpuThreads": 32,
        "hardware.memorySize": 256 * GIB,
        "runtime.powerState": "poweredOn",
        "config.product.version": "8.0.3",
        "datastore": ["vim.Datastore:ds-1", "vim.Datastore:ds-shared"],
        "vm": ["vim.VirtualMachine:vm-10", "vim.VirtualMachine:vm-11"],
    },
    "vim.HostSystem:host-2": {
        "name": "esx-02",
        "parent": "vim.ClusterComputeResource:domain-c7",
        "hardware.cpuInfo.numCpuCores": 16,
        "hardware.cpuInfo.numCpuThreads": 32,
        "hardware.memorySize": 256 * GIB,
        # switched off, and still mounting the shared san
        "runtime.powerState": "poweredOff",
        "datastore": ["vim.Datastore:ds-shared"],
        "vm": ["vim.VirtualMachine:vm-12"],
    },
    "vim.HostSystem:host-3": {
        "name": "esx-standalone",
        "parent": "vim.ComputeResource:domain-s9",
        "hardware.cpuInfo.numCpuCores": 8,
        "hardware.cpuInfo.numCpuThreads": 16,
        "hardware.memorySize": 64 * GIB,
        "runtime.powerState": "poweredOn",
        "datastore": ["vim.Datastore:ds-shared"],
        "vm": ["vim.VirtualMachine:vm-13"],
    },
}

DATASTORES = {
    "vim.Datastore:ds-1": {
        "name": "local-01",
        "summary.capacity": 2048 * GIB,
        "summary.freeSpace": 1024 * GIB,
        "summary.uncommitted": 512 * GIB,
        "summary.type": "VMFS",
    },
    "vim.Datastore:ds-shared": {
        "name": "san-gold",
        "summary.capacity": 8192 * GIB,
        "summary.freeSpace": 4096 * GIB,
        "summary.uncommitted": 0,
        "summary.type": "NFS",
    },
    "vim.Datastore:ds-orphan": {
        "name": "retired-array",
        "summary.capacity": 1024 * GIB,
        "summary.freeSpace": 1024 * GIB,
    },
}

VMS = {
    "vim.VirtualMachine:vm-10": {
        "name": "app-01",
        "runtime.host": "vim.HostSystem:host-1",
        "runtime.powerState": "poweredOn",
        "config.hardware.numCPU": 4,
        "config.hardware.memoryMB": 16384,
        "summary.storage.committed": 40 * GIB,
        "summary.storage.uncommitted": 60 * GIB,
    },
    "vim.VirtualMachine:vm-11": {
        "name": "old-jenkins",
        "runtime.host": "vim.HostSystem:host-1",
        "runtime.powerState": "poweredOff",
        "config.hardware.numCPU": 8,
        "config.hardware.memoryMB": 32768,
        "summary.storage.committed": 200 * GIB,
        "summary.storage.uncommitted": 0,
    },
    "vim.VirtualMachine:vm-12": {
        "name": "ubuntu-template",
        "runtime.host": "vim.HostSystem:host-2",
        "runtime.powerState": "poweredOff",
        "config.template": True,
        "config.hardware.numCPU": 2,
        "config.hardware.memoryMB": 4096,
        "summary.storage.committed": 20 * GIB,
        "summary.storage.uncommitted": 0,
    },
    "vim.VirtualMachine:vm-13": {
        "name": "build-runner",
        "runtime.host": "vim.HostSystem:host-3",
        "runtime.powerState": "poweredOn",
        "config.hardware.numCPU": 2,
        "config.hardware.memoryMB": 8192,
        "summary.storage.committed": 10 * GIB,
        "summary.storage.uncommitted": 10 * GIB,
    },
    # on no host vcenter will name: inaccessible, and its config never came back
    "vim.VirtualMachine:vm-99": {
        "name": "ghost",
        "runtime.powerState": "poweredOff",
        "summary.storage.committed": 5 * GIB,
    },
}


@pytest.fixture
def site():
    return build_site(CLUSTERS, HOSTS, DATASTORES, VMS, ANCESTRY, DATACENTERS)


def test_pools_are_clusters_plus_standalone_hosts(site):
    # qualified by datacenter, because two of them may each hold a "prod-gen11"
    assert {pool.key for pool in site.pools} == {"DC0/prod-gen11", "DC0/esx-standalone"}
    cluster = site.pool("prod-gen11")
    assert cluster.kind == "cluster"
    assert [host.name for host in cluster.hosts] == ["esx-01", "esx-02"]
    assert site.pool("esx-standalone").kind == "host"


def test_pool_is_unqualified_when_the_datacenter_is_unknown():
    # no ancestry handed in, e.g. a backend that has no folders at all
    site = build_site(CLUSTERS, HOSTS, DATASTORES, VMS)
    assert {pool.key for pool in site.pools} == {"prod-gen11", "esx-standalone"}


def test_vm_carries_the_host_name_not_a_moref(site):
    vms = {vm.name: vm for pool in site.pools for vm in pool.vms}
    assert vms["app-01"].host == "esx-01"
    assert vms["build-runner"].host == "esx-standalone"


def test_capacity_counts_cores_and_installed_iron(site):
    capacity = site.pool("prod-gen11").capacity()
    # 2 x 16 physical cores, never the 64 threads
    assert capacity["vcpu"] == Decimal(32)
    assert capacity["ram_gib"] == Decimal(512)
    # local-01 + san-gold, each once, however many hosts mount them
    assert capacity["storage_gib"] == Decimal(2048 + 8192)


def test_a_powered_off_host_is_capacity_and_a_finding(site):
    cluster = site.pool("prod-gen11")
    # it is in the capex and holds its licenses, so it counts...
    assert sum(host.cores for host in cluster.hosts) == 32
    # ...and it is named, because paying for an idle host is the point of the report
    assert cluster.hosts_powered_off == ("esx-02",)


def test_shared_datastore_is_flagged_in_both_pools(site):
    assert site.pool("prod-gen11").shared_datastores == ("san-gold",)
    assert site.pool("esx-standalone").shared_datastores == ("san-gold",)
    # the whole object, not the name: the waste pass prices its unusable capacity
    assert [ds.name for ds in site.unmounted_datastores] == ["retired-array"]
    assert site.unmounted_datastores[0].capacity_gib == Decimal(1024)


def test_same_named_datastores_in_two_pools_are_not_shared():
    """vcsim and real estates both hand out a "LocalDS_0" per datacenter.

    Sharing is a moref question. Keyed by name, every local datastore on the floor would
    come back flagged and the warning would mean nothing.
    """
    hosts = {
        "vim.HostSystem:host-1": {
            "name": "esx-a",
            "hardware.cpuInfo.numCpuCores": 8,
            "hardware.memorySize": 64 * GIB,
            "datastore": ["vim.Datastore:ds-11"],
        },
        "vim.HostSystem:host-2": {
            "name": "esx-b",
            "hardware.cpuInfo.numCpuCores": 8,
            "hardware.memorySize": 64 * GIB,
            "datastore": ["vim.Datastore:ds-22"],
        },
    }
    datastores = {
        ds_id: {"name": "LocalDS_0", "summary.capacity": 1024 * GIB, "summary.freeSpace": 0}
        for ds_id in ("vim.Datastore:ds-11", "vim.Datastore:ds-22")
    }
    site = build_site({}, hosts, datastores, {})
    assert [pool.shared_datastores for pool in site.pools] == [(), ()]


def test_provisioned_charges_cpu_and_ram_only_while_running(site):
    vms = {vm.name: vm for pool in site.pools for vm in pool.vms}

    running = vms["app-01"].provisioned()
    assert running == {
        "vcpu": Decimal(4),
        "ram_gib": Decimal(16),
        # 40 gib written of the 100 it may grow into: the blocks it occupies are the
        # bill, the promise is a risk and `thin-overcommit` is where it belongs
        "disk_gib": Decimal(40),
    }
    assert vms["app-01"].disk_gib == Decimal(100)

    # powered off: the ram is not reserved, another vm is using it. the disk is
    off = vms["old-jenkins"].provisioned()
    assert off == {"vcpu": Decimal(0), "ram_gib": Decimal(0), "disk_gib": Decimal(200)}

    template = vms["ubuntu-template"]
    assert template.template is True
    assert template.provisioned()["vcpu"] == Decimal(0)
    assert template.provisioned()["disk_gib"] == Decimal(20)


def test_orphan_vm_is_reported_not_priced(site):
    assert [vm.name for vm in site.orphan_vms] == ["ghost"]
    assert "ghost" not in {vm.name for pool in site.pools for vm in pool.vms}
    # no config came back, so it is flagged rather than silently charged as a 0-cpu vm
    assert site.orphan_vms[0].incomplete is True


def test_absent_properties_do_not_break_the_pass(site):
    hosts = {host.name: host for host in site.pool("prod-gen11").hosts}
    assert hosts["esx-01"].version == "8.0.3"
    assert hosts["esx-02"].version is None  # never came back, which is normal
    assert site.pool("esx-standalone").datastores[0].kind == "NFS"


def test_datastore_overcommit_is_reported(site):
    local = next(ds for ds in site.pool("prod-gen11").datastores if ds.name == "local-01")
    # 1024 used + 512 the thin disks may still claim, against 2048
    assert local.provisioned_gib == Decimal(1536)
    assert local.overcommit == Decimal("0.75")


def test_a_host_with_no_cores_is_a_failed_pass():
    broken = {"vim.HostSystem:host-1": {"name": "esx-01", "hardware.memorySize": 64 * GIB}}
    with pytest.raises(ConfigError, match="esx-01"):
        build_site({}, broken, {}, {})


def test_a_datastore_with_no_capacity_is_a_failed_pass():
    broken = {"vim.Datastore:ds-1": {"name": "local-01", "summary.freeSpace": 0}}
    with pytest.raises(ConfigError, match="local-01"):
        build_site({}, {}, broken, {})


def test_payload_feeds_the_allocator(site):
    card = {
        "rate_card": {"hardware_amortization": 10000, "power_and_cooling": 2000},
        "weights": {"cpu": 0.5, "ram": 0.3, "storage": 0.2},
    }
    result = allocate(site.pool("prod-gen11").allocation_payload(card))

    assert result["pool_monthly"] == 12000
    # keyed by moref: vcenter lets two vms share a name, and a duplicate key would make
    # the whole pool unpriceable
    assert set(result["vms"]) == {
        "vim.VirtualMachine:vm-10",
        "vim.VirtualMachine:vm-11",
        "vim.VirtualMachine:vm-12",
    }
    assert set(site.pool("prod-gen11").labels().values()) == {
        "app-01",
        "old-jenkins",
        "ubuntu-template",
    }
    # no measured usage yet, so nothing is charged as waste - that is the perf pass
    assert result["total_used"] == result["total_provisioned"]
    assert all(vm["waste"] == 0 for vm in result["vms"].values())
    # 4 of 32 cores asked for, 16 of 512 gib, and 260 of 10240 gib of disk occupied
    # (320 promised, which is the thin risk and not a charge)
    assert result["overcommit"]["vcpu"] == pytest.approx(4 / 32)
    assert result["overcommit"]["ram"] == pytest.approx(16 / 512)
    assert result["overcommit"]["storage"] == pytest.approx(260 / 10240)
    assert result["headroom"] > 0


# a vm with two snapshots: before-upgrade, and a veeam leftover taken on top of it
# file keys: 0 vmx, 1+2 base disk, 3+4 the delta before-upgrade started, 5+6 the live delta
SNAP_VM = {
    "name": "db-01",
    "runtime.host": "vim.HostSystem:host-1",
    "runtime.powerState": "poweredOn",
    "config.hardware.numCPU": 2,
    "config.hardware.memoryMB": 4096,
    "summary.storage.committed": 60 * GIB,
    "runtime.consolidationNeeded": True,
    "config.files.vmPathName": "[local-01] db-01/db-01.vmx",
    "snapshot": {
        "currentSnapshot": "vim.vm.Snapshot:snapshot-2",
        "rootSnapshotList": [
            {
                "snapshot": "vim.vm.Snapshot:snapshot-1",
                "name": "before-upgrade",
                "id": 1,
                "createTime": "2026-10-01T08:00:00+00:00",
                "quiesced": False,
                "childSnapshotList": [
                    {
                        "snapshot": "vim.vm.Snapshot:snapshot-2",
                        "name": "VEEAM BACKUP TEMPORARY SNAPSHOT",
                        "id": 2,
                        "createTime": "2026-11-01T02:00:00",
                        "quiesced": True,
                    }
                ],
            }
        ],
    },
    "layoutEx.file": [
        {"key": 0, "type": "config", "size": GIB},
        {"key": 1, "type": "diskDescriptor", "size": 0},
        {"key": 2, "type": "diskExtent", "size": 40 * GIB},
        {"key": 3, "type": "diskDescriptor", "size": 0},
        {"key": 4, "type": "diskExtent", "size": 8 * GIB},
        {"key": 5, "type": "diskDescriptor", "size": 0},
        {"key": 6, "type": "diskExtent", "size": 2 * GIB},
        {"key": 10, "type": "snapshotData", "size": GIB},
        {"key": 11, "type": "snapshotData", "size": 0},
        {"key": 12, "type": "snapshotMemory", "size": 4 * GIB},
    ],
    "layoutEx.snapshot": [
        {
            "key": "vim.vm.Snapshot:snapshot-1",
            "dataKey": 10,
            # key 0 is the vmx, never a snapshot's memory
            "memoryKey": 0,
            "disk": [{"key": 2000, "chain": [{"fileKey": [1, 2]}]}],
        },
        {
            "key": "vim.vm.Snapshot:snapshot-2",
            "dataKey": 11,
            "memoryKey": 12,
            "disk": [{"key": 2000, "chain": [{"fileKey": [1, 2]}, {"fileKey": [3, 4]}]}],
        },
    ],
    "layoutEx.disk": [
        {
            "key": 2000,
            "chain": [{"fileKey": [1, 2]}, {"fileKey": [3, 4]}, {"fileKey": [5, 6]}],
        }
    ],
}


def _one_vm(props):
    hosts = {"vim.HostSystem:host-1": {**HOSTS["vim.HostSystem:host-1"], "vm": ["vim.VirtualMachine:vm-50"]}}
    site = build_site({}, hosts, DATASTORES, {"vim.VirtualMachine:vm-50": props})
    return site.vms()[0]


def test_snapshot_tree_is_flattened_root_first_with_its_path():
    vm = _one_vm(SNAP_VM)
    assert [snap.path for snap in vm.snapshots] == [
        "before-upgrade",
        "before-upgrade / VEEAM BACKUP TEMPORARY SNAPSHOT",
    ]
    first, second = vm.snapshots
    assert first.uid == "vim.vm.Snapshot:snapshot-1"
    assert first.created == datetime(2026, 10, 1, 8, tzinfo=UTC)
    # a naive time is vcenter's utc
    assert second.created == datetime(2026, 11, 1, 2, tzinfo=UTC)
    assert (first.quiesced, second.quiesced) == (False, True)
    assert vm.consolidation_needed is True
    assert vm.vmx_path == "[local-01] db-01/db-01.vmx"


def test_a_snapshot_is_sized_by_the_delta_it_started_plus_its_own_files():
    first, second = _one_vm(SNAP_VM).snapshots
    # the 8 gib delta after its chain + its 1 gib vmsn, the vmx behind memoryKey 0 is not it
    assert first.size_gib == Decimal(9)
    # the live 2 gib delta + 4 gib vmem
    assert second.size_gib == Decimal(6)


def test_a_snapshot_with_no_layout_is_kept_at_size_zero():
    props = {k: v for k, v in SNAP_VM.items() if not k.startswith("layoutEx")}
    vm = _one_vm(props)
    assert len(vm.snapshots) == 2
    assert all(snap.size_gib == 0 for snap in vm.snapshots)


def test_missing_snapshot_paths_are_empty_not_errors(site):
    vm = next(vm for vm in site.vms() if vm.name == "app-01")
    assert vm.snapshots == ()
    assert vm.consolidation_needed is False
    assert vm.vmx_path is None
    # an empty tree or a garbled create time is tolerated the same way
    odd = {**SNAP_VM, "snapshot": {"rootSnapshotList": []}}
    assert _one_vm(odd).snapshots == ()
    garbled = {
        **SNAP_VM,
        "snapshot": {"rootSnapshotList": [{"name": "x", "createTime": "not a date"}]},
    }
    assert _one_vm(garbled).snapshots[0].created is None


def test_datastore_carries_the_hosts_that_mount_it():
    datastores = {
        "vim.Datastore:ds-1": {
            **DATASTORES["vim.Datastore:ds-1"],
            "host": [
                {"key": "vim.HostSystem:host-1", "mountInfo": {"path": "/vmfs/volumes/x"}},
                {"key": "vim.HostSystem:host-77"},  # a host this pass never saw
            ],
        }
    }
    site = build_site({}, {}, datastores, {})
    assert site.datastores[0].mount_hosts == ("vim.HostSystem:host-1", "vim.HostSystem:host-77")
    plain = build_site({}, {}, DATASTORES, {})
    assert all(ds.mount_hosts == () for ds in plain.datastores)


def test_layout_and_free_space_timestamps_are_read():
    at = datetime(2026, 11, 20, 10, tzinfo=UTC)
    vm = _one_vm({**SNAP_VM, "layoutEx.timestamp": at, "summary.storage.timestamp": "2026-11-20T09:50:00"})
    # the older of the two, a naive time is utc
    assert vm.layout_at == datetime(2026, 11, 20, 9, 50, tzinfo=UTC)
    assert _one_vm({**SNAP_VM, "layoutEx.timestamp": at}).layout_at == at
    assert _one_vm(SNAP_VM).layout_at is None
    datastores = {"vim.Datastore:ds-1": {**DATASTORES["vim.Datastore:ds-1"], "info.timestamp": at}}
    assert build_site({}, {}, datastores, {}).datastores[0].free_at == at
    assert build_site({}, {}, DATASTORES, {}).datastores[0].free_at is None


def test_the_wire_turns_data_objects_into_plain_dicts():
    vim = pytest.importorskip("pyVmomi").vim
    from clont.providers.onprem.vsphere import _plain

    created = datetime(2026, 10, 1, tzinfo=UTC)
    tree = vim.vm.SnapshotTree(
        snapshot=vim.vm.Snapshot("snapshot-1"),
        name="before-upgrade",
        id=1,
        createTime=created,
        quiesced=False,
        childSnapshotList=[],
    )
    out = _plain(vim.vm.SnapshotInfo(rootSnapshotList=[tree]))
    node = out["rootSnapshotList"][0]
    assert node["snapshot"] == "vim.vm.Snapshot:snapshot-1"
    assert node["createTime"] == created
    assert (node["name"], node["id"], node["quiesced"]) == ("before-upgrade", 1, False)
    assert "dynamicType" not in node
    layout = _plain(
        vim.vm.FileLayoutEx.SnapshotLayout(
            key=vim.vm.Snapshot("snapshot-1"),
            dataKey=3,
            disk=[vim.vm.FileLayoutEx.DiskLayout(key=2000, chain=[vim.vm.FileLayoutEx.DiskUnit(fileKey=[1, 2])])],
        )
    )
    assert layout["disk"][0]["chain"][0]["fileKey"] == [1, 2]


def test_browse_results_become_one_row_per_file_with_its_full_path():
    from clont.providers.onprem.inventory import datastore_files

    when = datetime(2026, 11, 1, tzinfo=UTC)
    results = [
        {"folderPath": "[LocalDS_0]", "file": [{"path": "loose.vmdk", "fileSize": 1024**3}]},
        {
            "folderPath": "[LocalDS_0] web-1/",
            "file": [
                {"path": "web-1.vmx", "fileSize": 4096, "modification": when},
                {"path": "web-1-flat.vmdk", "fileSize": 10 * 1024**3, "modification": when},
            ],
        },
        {"folderPath": "[LocalDS_0] old", "file": [{"path": "old.vmdk"}, {"fileSize": 9}]},
        # vcsim's shape, no space and no trailing slash
        {"folderPath": "[LocalDS_0]/sim", "file": [{"path": "sim.vmx"}]},
    ]
    rows = datastore_files("vim.Datastore:ds-1", results)
    assert [row.path for row in rows] == [
        "[LocalDS_0] loose.vmdk",
        "[LocalDS_0] old/old.vmdk",
        "[LocalDS_0] sim/sim.vmx",
        "[LocalDS_0] web-1/web-1-flat.vmdk",
        "[LocalDS_0] web-1/web-1.vmx",
    ]
    flat = rows[3]
    assert flat.size_gib == Decimal(10) and flat.modified == when
    assert flat.folder == "[LocalDS_0] web-1"
    # no size and no time come back as zero and unknown, never a guess
    assert rows[1].size_gib == 0 and rows[1].modified is None
    assert datastore_files("vim.Datastore:ds-1", None) == ()


class _Task:
    def __init__(self, states):
        self._states = list(states)

    @property
    def info(self):
        state, result, error = self._states.pop(0) if len(self._states) > 1 else self._states[0]
        return type("Info", (), {"state": state, "result": result, "error": error})()


class _Ds:
    def __init__(self, moid, name, task=None, raises=None, accessible=True):
        self._moId, self.name = moid, name
        self.summary = type("S", (), {"accessible": accessible})()
        outer = self

        class _Browser:
            def SearchDatastoreSubFolders_Task(self, datastorePath, searchSpec):
                outer.asked = (datastorePath, list(searchSpec.matchPattern))
                if raises:
                    raise raises
                return task

        self.browser = _Browser()


def _browsing(monkeypatch, datastores):
    vim = pytest.importorskip("pyVmomi").vim
    from clont.providers.onprem import vsphere

    session = vsphere.VsphereInventory("vc.lab", "ro", "x")
    session._si, session._vim = object(), vim
    monkeypatch.setattr(session, "_entities", lambda _type: datastores)
    monkeypatch.setattr(vsphere, "_moref", lambda obj: f"vim.Datastore:{obj._moId}")
    monkeypatch.setattr(vsphere.time, "sleep", lambda _s: None)
    return vim, session


def test_browse_lists_files_and_skips_what_it_cannot_read(monkeypatch, caplog):
    vim = pytest.importorskip("pyVmomi").vim
    state = vim.TaskInfo.State
    folder = vim.host.DatastoreBrowser.SearchResults(
        folderPath="[a] vm/",
        file=[vim.host.DatastoreBrowser.FileInfo(path="vm.vmdk", fileSize=1024**3)],
    )
    good = _Ds("ds-1", "a", _Task([(state.running, None, None), (state.success, [folder], None)]))
    denied = _Ds("ds-2", "b", raises=vim.fault.NoPermission(privilegeId="Datastore.Browse"))
    denied_late = _Ds("ds-3", "c", _Task([(state.error, None, vim.fault.NoPermission())]))
    broken = _Ds("ds-4", "d", _Task([(state.error, None, vim.fault.FileNotFound())]))
    offline = _Ds("ds-5", "e", accessible=False)
    _, session = _browsing(monkeypatch, [good, denied, denied_late, broken, offline])

    files = session.browse()
    assert list(files) == ["vim.Datastore:ds-1"]
    assert [row.path for row in files["vim.Datastore:ds-1"]] == ["[a] vm/vm.vmdk"]
    assert good.asked == ("[a]", ["*.vmdk", "*.vmx"])
    # one warning names the grant and every datastore it cost
    grant = [r.getMessage() for r in caplog.records if "Datastore.Browse" in r.getMessage()]
    assert len(grant) == 1 and "b, c" in grant[0]


def test_a_browse_that_never_finishes_leaves_the_datastore_out(monkeypatch, caplog):
    vim = pytest.importorskip("pyVmomi").vim
    slow = _Ds("ds-1", "slow", _Task([(vim.TaskInfo.State.running, None, None)]))
    _, session = _browsing(monkeypatch, [slow])
    assert session.browse(timeout_seconds=0) == {}
    assert any("timed out on slow" in r.getMessage() for r in caplog.records)


def test_known_denied_datastores_are_not_asked_again(monkeypatch, caplog):
    vim = pytest.importorskip("pyVmomi").vim
    denied = _Ds("ds-2", "b", raises=vim.fault.NoPermission(privilegeId="Datastore.Browse"))
    _, session = _browsing(monkeypatch, [denied])
    assert session.browse() == {} and session.browse_denied == ["b"]
    caplog.clear()
    del denied.asked
    assert session.browse(skip=frozenset({"b"})) == {}
    # skipped, so no task, no warning and nothing newly denied
    assert not hasattr(denied, "asked")
    assert session.browse_denied == []
    assert not [r for r in caplog.records if "Datastore.Browse" in r.getMessage()]


def _granting(monkeypatch, datastores, answer):
    vim, session = _browsing(monkeypatch, datastores)
    asked = {}

    class _Auth:
        def HasPrivilegeOnEntities(self, entity, sessionId, privId):
            asked.update(entity=[e.name for e in entity], session=sessionId, priv=privId)
            if isinstance(answer, Exception):
                raise answer
            return answer

    session._content = type(
        "C",
        (),
        {
            "authorizationManager": _Auth(),
            "sessionManager": type(
                "S", (), {"currentSession": type("K", (), {"key": "s-1"})()}
            )(),
        },
    )()
    monkeypatch.setattr(vim.ManagedEntity, "Array", lambda items: list(items))
    return vim, session, asked


def test_browse_grants_ask_once_for_every_datastore(monkeypatch):
    vim = pytest.importorskip("pyVmomi").vim
    a, b = _Ds("ds-1", "a"), _Ds("ds-2", "b")
    avail = vim.AuthorizationManager.PrivilegeAvailability

    def row(ds, granted):
        grant = avail(privId="Datastore.Browse", isGranted=granted)
        return type("R", (), {"entity": ds, "privAvailability": [grant]})()

    _, session, asked = _granting(monkeypatch, [a, b], [row(a, True), row(b, False)])
    assert session.browse_grants() == {"a": True, "b": False}
    assert asked == {"entity": ["a", "b"], "session": "s-1", "priv": ["Datastore.Browse"]}


def test_browse_grants_say_none_when_the_host_cannot_answer(monkeypatch):
    vmodl = pytest.importorskip("pyVmomi").vmodl
    _, session, _ = _granting(monkeypatch, [_Ds("ds-1", "a")], vmodl.fault.NotSupported())
    assert session.browse_grants() is None
    _, session, _ = _granting(monkeypatch, [], [])
    assert session.browse_grants() == {}


def test_layout_files_are_the_edges_and_unseen_mounts_are_named():
    from clont.providers.onprem.inventory import build_site

    hosts = {
        "h-1": {
            "name": "esx-01",
            "hardware.cpuInfo.numCpuCores": 4,
            "hardware.memorySize": 16 * 1024**3,
            "datastore": ["ds-1"],
            "vm": ["vm-1"],
        }
    }
    datastores = {
        "ds-1": {
            "name": "san-01",
            "summary.capacity": 100 * 1024**3,
            "host": [{"key": "h-1"}, {"key": "h-other"}],
        }
    }
    vms = {
        "vm-1": {
            "name": "web-1",
            "layoutEx.file": [
                {"key": 0, "name": "[san-01] web-1/web-1.vmx", "type": "config", "size": 4096},
                {"key": 1, "name": "[san-01] web-1/web-1-flat.vmdk", "size": 2 * 1024**3},
                {"key": 2, "type": "log"},
            ],
        }
    }
    site = build_site({}, hosts, datastores, vms)
    [vm] = site.vms()
    # a row with no name can't be an edge, so it is not one
    assert [f.path for f in vm.files] == ["[san-01] web-1/web-1.vmx", "[san-01] web-1/web-1-flat.vmdk"]
    assert vm.files[1].size_gib == 2
    assert site.datastores[0].unseen_hosts == ("h-other",)


# template clone history: vcenter's events, plain dicts in, a per-template answer out

NOW = datetime(2026, 11, 24, tzinfo=UTC)
SINCE = datetime(2026, 10, 25, tzinfo=UTC)  # 30 days of history
GOLDEN, SPARE = "vim.VirtualMachine:vm-20", "vim.VirtualMachine:vm-21"
TEMPLATES = {GOLDEN: "golden-ubuntu", SPARE: "spare-win"}


def _event(kind, day, **args):
    return {"type": f"vim.event.{kind}", "createdTime": datetime(2026, 11, day, tzinfo=UTC), **args}


def test_a_clone_or_deploy_from_a_template_is_its_last_use():
    from clont.providers.onprem.inventory import clone_history

    events = [
        _event("VmClonedEvent", 3, vm={"vm": "vim.VirtualMachine:vm-30", "name": "web-1"},
               sourceVm={"vm": GOLDEN, "name": "golden-ubuntu"}),
        _event("VmDeployedEvent", 9, vm={"vm": "vim.VirtualMachine:vm-31", "name": "web-2"},
               srcTemplate={"vm": GOLDEN, "name": "golden-ubuntu"}),
        # a clone of something else entirely
        _event("VmClonedEvent", 10, sourceVm={"vm": "vim.VirtualMachine:vm-99", "name": "x"}),
    ]
    found = clone_history(events, SINCE, TEMPLATES, now=NOW)
    assert found.days == 30
    assert found.last_cloned == {GOLDEN: datetime(2026, 11, 9, tzinfo=UTC)}
    assert found.too_young == frozenset()


def test_a_template_that_arrived_inside_the_window_is_too_young():
    from clont.providers.onprem.inventory import clone_history

    events = [
        _event("VmRegisteredEvent", 2, vm={"vm": SPARE, "name": "spare-win"}),
        # cloned into a template: the new one is the `vm`, the source is not new
        _event("VmClonedEvent", 4, vm={"vm": GOLDEN, "name": "golden-ubuntu"},
               sourceVm={"vm": "vim.VirtualMachine:vm-5", "name": "base"}),
    ]
    found = clone_history(events, SINCE, TEMPLATES, now=NOW)
    assert found.too_young == {GOLDEN, SPARE}
    assert found.last_cloned == {}
    # a create date inside the window says the same, with no event at all
    late = {SPARE: datetime(2026, 11, 1, tzinfo=UTC), GOLDEN: datetime(2025, 1, 1, tzinfo=UTC)}
    assert clone_history([], SINCE, TEMPLATES, created=late, now=NOW).too_young == {SPARE}


def test_an_event_with_no_moref_matches_by_name_and_only_then():
    from clont.providers.onprem.inventory import clone_history

    twins = {GOLDEN: "golden", SPARE: "golden"}
    events = [_event("VmBeingClonedEvent", 5, vm={"name": "golden"})]
    # a name may be two templates, both lose the "never", never the other way round
    assert set(clone_history(events, SINCE, twins, now=NOW).last_cloned) == {GOLDEN, SPARE}
    # a moref that is not a template is not rescued by a matching name
    events = [_event("VmClonedEvent", 5, sourceVm={"vm": "vim.VirtualMachine:vm-9", "name": "golden"})]
    assert clone_history(events, SINCE, twins, now=NOW).last_cloned == {}


class _Collector:
    def __init__(self, events, page_log):
        self._events, self._pos, self._log = list(events), 0, page_log
        self.destroyed = False

    def RewindCollector(self):
        self._pos = 0

    def ReadNextEvents(self, maxCount):
        page = self._events[self._pos : self._pos + maxCount]
        self._pos += len(page)
        self._log.append(maxCount)
        return page

    def DestroyCollector(self):
        self.destroyed = True


class _VimEvent:
    def __init__(self, day, vm=None, source=None):
        self.createdTime = datetime(2026, 11, day, tzinfo=UTC)
        self.vm, self.sourceVm = vm, source


class _Arg:
    def __init__(self, moref, name):
        self.vm = type("VirtualMachine", (), {"_moId": moref})()
        self.name = name


_arg = _Arg


class VmClonedEvent(_VimEvent):
    pass


class DvsCreatedEvent(_VimEvent):
    pass


def _eventing(monkeypatch, *, api="VirtualCenter", events=(), fails=None):
    vim = pytest.importorskip("pyVmomi").vim
    from clont.providers.onprem import vsphere

    session = vsphere.VsphereInventory("vc.lab", "ro", "x")
    session._si, session._vim, session.api_type = object(), vim, api
    collectors: list[_Collector] = []
    pages: list[int] = []

    class _Manager:
        def CreateCollectorForEvents(self, filter):
            if fails is not None:
                raise fails
            # the empty filter is "every event", the oldest of them is the window start
            wanted = filter.eventTypeId or []
            rows = [e for e in events if not wanted or type(e).__name__ in wanted]
            collectors.append(_Collector(rows, pages))
            return collectors[-1]

    session._content = type("C", (), {"eventManager": _Manager()})()
    monkeypatch.setattr(session, "_created", lambda _templates: {})
    monkeypatch.setattr(vsphere, "_plain", _plain_stub)
    return session, collectors, pages


def _plain_stub(value):
    if hasattr(value, "_moId"):
        return f"vim.VirtualMachine:{value._moId}"
    if isinstance(value, datetime | str) or value is None:
        return value
    return {k: _plain_stub(v) for k, v in vars(value).items() if v is not None}


def test_the_wire_reads_the_window_and_the_clones_oldest_first(monkeypatch):
    events = [
        DvsCreatedEvent(1),
        VmClonedEvent(4, vm=_arg("vm-30", "web-1"), source=_arg("vm-20", "golden-ubuntu")),
    ]
    session, collectors, pages = _eventing(monkeypatch, events=events)
    found = session.clone_events({GOLDEN: "golden-ubuntu"})
    assert found is not None and found.since == datetime(2026, 11, 1, tzinfo=UTC)
    assert found.last_cloned == {GOLDEN: datetime(2026, 11, 4, tzinfo=UTC)}
    # one event for the window, then pages of clones, and every collector is gone after
    assert pages[0] == 1 and len(collectors) == 2
    assert all(c.destroyed for c in collectors)
    assert not session.clones_failed


def test_a_host_agent_has_no_history_and_gets_no_verdict(monkeypatch, caplog):
    session, collectors, _ = _eventing(monkeypatch, api="HostAgent", events=[DvsCreatedEvent(1)])
    assert session.clone_events({GOLDEN: "golden-ubuntu"}) is None
    assert collectors == [] and not caplog.records and not session.clones_failed


def test_a_failed_event_read_is_one_warning_and_no_verdict(monkeypatch, caplog):
    vim = pytest.importorskip("pyVmomi").vim
    session, _, _ = _eventing(monkeypatch, fails=vim.fault.NoPermission(privilegeId="System.View"))
    assert session.clone_events({GOLDEN: "golden-ubuntu"}) is None
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warned) == 1 and "no clone verdict" in warned[0]
    assert session.clones_failed


def test_too_many_clone_events_is_no_verdict_rather_than_half_of_one(monkeypatch, caplog):
    from clont.providers.onprem import vsphere

    monkeypatch.setattr(vsphere, "EVENT_LIMIT", 3)
    events = [DvsCreatedEvent(1)] + [
        VmClonedEvent(d, source=_arg("vm-20", "golden-ubuntu")) for d in range(2, 6)
    ]
    session, collectors, _ = _eventing(monkeypatch, events=events)
    assert session.clone_events({GOLDEN: "golden-ubuntu"}) is None
    assert session.clones_failed and all(c.destroyed for c in collectors)
    assert any("more than 3 clone events" in r.getMessage() for r in caplog.records)


def test_no_template_means_no_event_read(monkeypatch):
    session, collectors, _ = _eventing(monkeypatch, events=[DvsCreatedEvent(1)])
    assert session.clone_events({}) is None and collectors == []
