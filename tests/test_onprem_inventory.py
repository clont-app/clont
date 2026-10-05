"""The inventory transform: pools, capacity, and what a vm is charged for.

The wire itself is test layer 1 in the cft functests (vcsim). What is here is the pure
half — property dicts in, pools out — plus the handoff into `allocate()`, because the
whole point of the transform is to produce that payload.
"""

from __future__ import annotations

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
        "disk_gib": Decimal(100),  # committed + uncommitted, what it may grow into
    }

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
    assert set(result["vms"]) == {"app-01", "old-jenkins", "ubuntu-template"}
    # no measured usage yet, so nothing is charged as waste - that is the perf pass
    assert result["total_used"] == result["total_provisioned"]
    assert all(vm["waste"] == 0 for vm in result["vms"].values())
    # 4 of 32 cores asked for, 16 of 512 gib, 320 of 10240 gib disk
    assert result["overcommit"]["vcpu"] == pytest.approx(4 / 32)
    assert result["overcommit"]["ram"] == pytest.approx(16 / 512)
    assert result["overcommit"]["storage"] == pytest.approx(320 / 10240)
    assert result["headroom"] > 0
