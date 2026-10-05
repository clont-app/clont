"""The waste pass: one inventory snapshot in, priced findings out.

The rates themselves are already covered by the golden tables, so what is tested here is
what this layer decides: which things count as waste at all, that the money comes off the
operator's own card (and is therefore *not* flagged approximate), and that every
threshold can actually silence a finding.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from clont.core.models import Cloud, Period
from clont.finops.base import FinOpsTuning
from clont.finops.onprem.config import OnPremSite
from clont.finops.onprem.waste import OnPremWasteCollector, site_storage_rate
from clont.providers.onprem.inventory import Datastore, Pool, build_site

GIB = 1024**3
PERIOD = Period(start=date(2026, 10, 1), end=date(2026, 10, 4))

# one cluster of two identical hosts: 16 cores, 128 gib, one 2000 gib san, $7300 a month
# -> $0.73 per gib-month of storage, and cpu+ram carry 0.8 of the pool between them
CLUSTERS = {
    "vim.ClusterComputeResource:domain-c7": {
        "name": "prod",
        "host": ["vim.HostSystem:host-1", "vim.HostSystem:host-2"],
    }
}

HOSTS = {
    "vim.HostSystem:host-1": {
        "name": "esx-01",
        "hardware.cpuInfo.numCpuCores": 8,
        "hardware.memorySize": 64 * GIB,
        "runtime.powerState": "poweredOn",
        "datastore": ["vim.Datastore:ds-1"],
        "vm": ["vim.VirtualMachine:vm-10", "vim.VirtualMachine:vm-11"],
    },
    # same iron, powered on, nothing on it: the zombie
    "vim.HostSystem:host-2": {
        "name": "esx-02",
        "hardware.cpuInfo.numCpuCores": 8,
        "hardware.memorySize": 64 * GIB,
        "runtime.powerState": "poweredOn",
        "datastore": ["vim.Datastore:ds-1"],
    },
}

DATASTORES = {
    "vim.Datastore:ds-1": {
        "name": "san-01",
        "summary.capacity": 2000 * GIB,
        "summary.freeSpace": 1500 * GIB,  # 500 gib used, and the vms claim 160 of it
        "summary.uncommitted": 0,
    }
}

VMS = {
    "vim.VirtualMachine:vm-10": {
        "name": "app-01",
        "runtime.host": "vim.HostSystem:host-1",
        "runtime.powerState": "poweredOn",
        "config.hardware.numCPU": 4,
        "config.hardware.memoryMB": 8192,
        "summary.storage.committed": 60 * GIB,
        "summary.storage.uncommitted": 0,
    },
    "vim.VirtualMachine:vm-11": {
        "name": "old-01",
        "runtime.host": "vim.HostSystem:host-1",
        "runtime.powerState": "poweredOff",
        "config.hardware.numCPU": 4,
        "config.hardware.memoryMB": 8192,
        "summary.storage.committed": 100 * GIB,
        "summary.storage.uncommitted": 0,
    },
}

CARD = {
    "rate_card": {"hardware_amortization": 7300},
    "weights": {"cpu": 0.5, "ram": 0.3, "storage": 0.2},
}
STORAGE_RATE = 7300 * 0.2 / 2000  # $0.73 per gib-month


class FakeProvider:
    cloud = Cloud.ONPREM
    alias = "dc1"

    def __init__(self, site, inventory):
        self.site = site
        self._inventory = inventory

    def inventory(self, *, refresh: bool = False):
        return self._inventory


def inventory(clusters=None, hosts=None, datastores=None, vms=None):
    return build_site(
        CLUSTERS if clusters is None else clusters,
        HOSTS if hosts is None else hosts,
        DATASTORES if datastores is None else datastores,
        VMS if vms is None else vms,
    )


def advise(inv=None, tuning=None, site=None):
    provider = FakeProvider(site or OnPremSite(**CARD), inv or inventory())
    recs = OnPremWasteCollector(provider, tuning).recommendations(PERIOD)
    return {rec.kind: rec for rec in recs}, recs


def test_a_stopped_vm_is_billed_for_its_disk_and_nothing_else():
    found, _ = advise()
    stopped = found["stopped-vm"]
    assert stopped.resource.resource_id == "old-01"
    assert float(stopped.estimated_savings.amount) == pytest.approx(100 * STORAGE_RATE, abs=0.01)
    # its ram went back to the pool, so it must not be charged for it
    assert "ram is back in the pool" in stopped.summary


def test_the_operators_own_card_is_not_a_ballpark():
    found, _ = advise()
    assert found["stopped-vm"].approximate is False
    assert found["stopped-vm"].priced_region == "prod"
    # the leftovers have no pool, so they are priced at the site mean and say so
    assert found["unaccounted-storage"].approximate is True
    assert found["unaccounted-storage"].priced_region == "site"


def test_a_running_vm_is_never_a_finding():
    _, recs = advise()
    assert "app-01" not in {rec.resource.resource_id for rec in recs}


def test_a_zombie_host_costs_its_share_of_the_pool():
    found, _ = advise()
    zombie = found["zombie-host"]
    assert zombie.resource.resource_id == "esx-02"
    # half the cores and half the ram of the pool: 7300 * (0.5*0.5 + 0.3*0.5)
    assert float(zombie.estimated_savings.amount) == pytest.approx(7300 * 0.4, abs=0.01)
    # the datastores stay whether the host does or not
    assert "burning power" in zombie.summary


def test_a_host_with_only_a_stopped_vm_is_still_a_zombie():
    # old-01 is powered off, so host-1 carries no load either
    hosts = {
        key: {**props, "vm": ["vim.VirtualMachine:vm-11"]} if key.endswith("host-1") else props
        for key, props in HOSTS.items()
    }
    only_stopped = {"vim.VirtualMachine:vm-11": VMS["vim.VirtualMachine:vm-11"]}
    _, recs = advise(inventory(hosts=hosts, vms=only_stopped))
    zombies = {r.resource.resource_id for r in recs if r.kind == "zombie-host"}
    assert zombies == {"esx-01", "esx-02"}


def test_a_switched_off_host_is_a_different_finding_than_a_zombie():
    hosts = {
        key: ({**props, "runtime.powerState": "poweredOff"} if key.endswith("host-2") else props)
        for key, props in HOSTS.items()
    }
    found, _ = advise(inventory(hosts=hosts))
    assert "zombie-host" not in found
    off = found["powered-off-host"]
    # capacity counts installed iron, so the pool's rates already charge for it
    assert float(off.estimated_savings.amount) == pytest.approx(7300 * 0.4, abs=0.01)
    assert "still in the capex" in off.summary


def test_a_template_is_its_own_kind_so_it_can_be_ignored_wholesale():
    vms = {
        **VMS,
        "vim.VirtualMachine:vm-12": {
            "name": "golden-ubuntu",
            "runtime.host": "vim.HostSystem:host-1",
            "runtime.powerState": "poweredOff",
            "config.template": True,
            "config.hardware.numCPU": 2,
            "config.hardware.memoryMB": 4096,
            "summary.storage.committed": 40 * GIB,
            "summary.storage.uncommitted": 0,
        },
    }
    hosts = {
        key: ({**props, "vm": [*props["vm"], "vim.VirtualMachine:vm-12"]} if "vm" in props else props)
        for key, props in HOSTS.items()
    }
    found, _ = advise(inventory(hosts=hosts, vms=vms))
    assert found["template-disk"].resource.resource_id == "golden-ubuntu"
    assert float(found["template-disk"].estimated_savings.amount) == pytest.approx(
        40 * STORAGE_RATE, abs=0.01
    )


def test_storage_no_vm_accounts_for_is_sized_not_listed():
    found, _ = advise()
    # 500 gib used on the san, 160 gib claimed by the two vms
    gap = found["unaccounted-storage"]
    assert float(gap.estimated_savings.amount) == pytest.approx(340 * STORAGE_RATE, abs=0.01)
    assert "cannot browse a datastore" in gap.summary


def test_the_unaccounted_gap_needs_both_thresholds():
    # the gap is 340 gib and 68% of the used space, so either threshold can silence it
    assert "unaccounted-storage" not in advise(tuning=FinOpsTuning(onprem_unaccounted_min_gib=400))[0]
    assert "unaccounted-storage" not in advise(tuning=FinOpsTuning(onprem_unaccounted_min_pct=80))[0]


def test_a_deduping_array_holds_less_than_the_vms_claim_and_that_is_not_a_finding():
    datastores = {
        "vim.Datastore:ds-1": {**DATASTORES["vim.Datastore:ds-1"], "summary.freeSpace": 1950 * GIB}
    }
    assert "unaccounted-storage" not in advise(inventory(datastores=datastores))[0]


def test_a_shared_datastore_is_counted_once_across_pools():
    # two clusters mounting the same san: a per-pool subtraction would count it twice and
    # invent a second 500 gib of used space
    clusters = {
        **CLUSTERS,
        "vim.ClusterComputeResource:domain-c8": {"name": "dev", "host": ["vim.HostSystem:host-3"]},
    }
    hosts = {
        **HOSTS,
        "vim.HostSystem:host-3": {
            "name": "esx-03",
            "hardware.cpuInfo.numCpuCores": 8,
            "hardware.memorySize": 64 * GIB,
            "runtime.powerState": "poweredOn",
            "datastore": ["vim.Datastore:ds-1"],
        },
    }
    found, _ = advise(inventory(clusters=clusters, hosts=hosts))
    gap = found["unaccounted-storage"]
    # the gap is still 340 gib, counted off the site's flat list. both cards pay for the
    # same 2000 gib, so the site mean is twice a single pool's rate — the array is in two
    # pools' capacity and only exists once
    assert float(gap.estimated_savings.amount) == pytest.approx(340 * STORAGE_RATE * 2, abs=0.01)


def test_a_shared_arrays_thin_risk_is_reported_once():
    clusters = {
        **CLUSTERS,
        "vim.ClusterComputeResource:domain-c8": {"name": "dev", "host": ["vim.HostSystem:host-3"]},
    }
    hosts = {
        **HOSTS,
        "vim.HostSystem:host-3": {
            "name": "esx-03",
            "hardware.cpuInfo.numCpuCores": 8,
            "hardware.memorySize": 64 * GIB,
            "runtime.powerState": "poweredOn",
            "datastore": ["vim.Datastore:ds-1"],
        },
    }
    datastores = {
        "vim.Datastore:ds-1": {
            **DATASTORES["vim.Datastore:ds-1"],
            "summary.uncommitted": 3000 * GIB,
        }
    }
    _, recs = advise(inventory(clusters=clusters, hosts=hosts, datastores=datastores))
    # one array, one risk: two clusters mount it and neither can fix it twice
    thin = [rec for rec in recs if rec.kind == "thin-overcommit"]
    assert [rec.resource.resource_id for rec in thin] == ["san-01"]


def test_thin_overcommit_is_a_risk_and_prices_at_zero():
    datastores = {
        "vim.Datastore:ds-1": {
            **DATASTORES["vim.Datastore:ds-1"],
            "summary.uncommitted": 3000 * GIB,  # 3500 promised on 2000
        }
    }
    found, _ = advise(inventory(datastores=datastores))
    thin = found["thin-overcommit"]
    assert thin.estimated_savings.amount == Decimal("0.00")
    assert "1.75x" in thin.summary
    # under the ratio it is normal practice, not a finding
    assert "thin-overcommit" not in advise(
        inventory(datastores=datastores), FinOpsTuning(onprem_thin_overcommit_ratio=2.0)
    )[0]


def test_an_unmounted_datastore_and_an_orphan_vm_land_at_the_site():
    datastores = {
        **DATASTORES,
        "vim.Datastore:ds-2": {
            "name": "old-array",
            "summary.capacity": 500 * GIB,
            "summary.freeSpace": 500 * GIB,
            "summary.uncommitted": 0,
        },
    }
    vms = {
        **VMS,
        "vim.VirtualMachine:vm-99": {
            "name": "lost-01",
            "runtime.powerState": "poweredOff",
            "config.hardware.numCPU": 2,
            "config.hardware.memoryMB": 4096,
            "summary.storage.committed": 80 * GIB,
            "summary.storage.uncommitted": 0,
        },
    }
    found, _ = advise(inventory(datastores=datastores, vms=vms))
    # the site rate is capacity-weighted, and the unmounted array is in no pool's capacity
    assert float(found["unmounted-datastore"].estimated_savings.amount) == pytest.approx(
        500 * STORAGE_RATE, abs=0.01
    )
    assert found["orphan-vm"].resource.resource_id == "lost-01"
    assert found["orphan-vm"].resource.region == "site"


def test_the_savings_floor_silences_the_small_stuff():
    found, _ = advise(tuning=FinOpsTuning(onprem_min_savings_usd=10_000))
    # every priced finding is gone, the risk that has no price stays
    assert set(found) == set()


def test_a_pool_nobody_could_price_is_skipped_not_fatal():
    broken = {
        "vim.Datastore:ds-1": {**DATASTORES["vim.Datastore:ds-1"], "summary.capacity": 1 * GIB}
    }
    # one gib of storage still prices; what cannot price is a pool with no datastore at all
    hosts = {key: {**props, "datastore": []} for key, props in HOSTS.items()}
    found, recs = advise(inventory(hosts=hosts, datastores=broken))
    assert recs == []
    assert found == {}


def test_the_site_rate_is_weighted_by_capacity_not_averaged_over_pools():
    small = (_pool("ds-a", 100), _priced(100, 10))
    large = (_pool("ds-b", 900), _priced(900, 1))
    # 1000 gib and 1900 dollars of storage money
    assert site_storage_rate([small, large]) == Decimal("1.9")
    assert site_storage_rate([]) is None


def test_the_site_rate_divides_by_each_array_once():
    san = _pool("ds-shared", 1000)
    priced = _priced(1000, 1)
    # the same san in two pools: $2000 of storage money buys 1000 gib, not 2000
    assert site_storage_rate([(san, priced), (san, priced)]) == Decimal("2")


def _priced(gib: float, rate: float) -> dict:
    return {"capacity": {"storage_gib": gib}, "rates": {"storage_gib_month": rate}}


def _pool(ds_uid: str, gib: int) -> Pool:
    store = Datastore(
        uid=ds_uid, name=ds_uid, capacity_gib=Decimal(gib), free_gib=Decimal(0),
        provisioned_gib=Decimal(gib),
    )
    return Pool(name="p", kind="cluster", datacenter=None, hosts=(), datastores=(store,), vms=())
