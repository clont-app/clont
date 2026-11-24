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
