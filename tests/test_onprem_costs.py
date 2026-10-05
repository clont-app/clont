"""The emission layer: pools and cards in, `CostRecord`s out.

The arithmetic is already covered by the golden tables (rates, card merge), so what is
tested here is only what the collector itself decides: the day share, one record per vm,
the headroom line that makes a site's total equal what the operator actually pays, and
what happens to a pool that cannot be priced.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from clont.core.errors import ConfigError
from clont.core.models import Cloud, Period
from clont.finops.onprem.config import InventoryConfig, OnPremSite
from clont.finops.onprem.costs import OnPremCostCollector
from clont.providers.onprem.inventory import build_site
from clont.providers.onprem.metrics import Usage
from clont.providers.onprem.provider import OnPremProvider

GIB = 1024**3
DAY = date(2026, 10, 4)
PERIOD = Period(start=date(2026, 10, 1), end=DAY)
DAY_SHARE = 24 / 730

DATACENTERS = {"vim.Datacenter:datacenter-2": {"name": "DC0"}}
ANCESTRY = {"vim.Folder:group-h4": {"name": "host", "parent": "vim.Datacenter:datacenter-2"}}

CLUSTERS = {
    "vim.ClusterComputeResource:domain-c7": {
        "name": "prod",
        "parent": "vim.Folder:group-h4",
        "host": ["vim.HostSystem:host-1"],
    }
}

HOSTS = {
    "vim.HostSystem:host-1": {
        "name": "esx-01",
        "parent": "vim.ClusterComputeResource:domain-c7",
        "hardware.cpuInfo.numCpuCores": 8,
        "hardware.cpuInfo.numCpuThreads": 16,
        "hardware.memorySize": 64 * GIB,
        "runtime.powerState": "poweredOn",
        "datastore": ["vim.Datastore:ds-1"],
        "vm": ["vim.VirtualMachine:vm-10", "vim.VirtualMachine:vm-11"],
    }
}

DATASTORES = {
    "vim.Datastore:ds-1": {
        "name": "local-01",
        "summary.capacity": 1000 * GIB,
        "summary.freeSpace": 500 * GIB,
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
        "summary.storage.uncommitted": 40 * GIB,
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

# 7300/month over 8 cores, 64 gib and 1000 gib: $0.625/vcpu-hour, $0.046875/gib-hour, $1.46/gib-month
CARD = {"rate_card": {"hardware_amortization": 7300}, "weights": {"cpu": 0.5, "ram": 0.3, "storage": 0.2}}
POOL_MONTHLY = 7300


class FakeProvider:
    """An `OnPremProvider` with the vcenter cut out: a pass is handed in, not fetched."""

    cloud = Cloud.ONPREM
    alias = "dc1"

    def __init__(self, site, inventory):
        self.site = site
        self._inventory = inventory
        self.passes = 0

    def inventory(self, *, refresh: bool = False):
        self.passes += 1
        return self._inventory


def site_inventory(clusters=None, hosts=None, datastores=None, vms=None):
    return build_site(
        CLUSTERS if clusters is None else clusters,
        HOSTS if hosts is None else hosts,
        DATASTORES if datastores is None else datastores,
        VMS if vms is None else vms,
        ANCESTRY,
        DATACENTERS,
    )


def collect(site_config, inventory=None):
    provider = FakeProvider(site_config, inventory or site_inventory())
    return OnPremCostCollector(provider).collect(PERIOD)


@pytest.fixture
def site_config():
    return OnPremSite(**CARD, clusters={"prod": {}})


def by_service(records):
    out: dict[str, list] = {}
    for record in records:
        out.setdefault(record.service, []).append(record)
    return out


def test_one_record_per_vm_plus_headroom(site_config):
    records = by_service(collect(site_config))
    assert {r.resource.resource_id for r in records["vm"]} == {"app-01", "old-01"}
    assert len(records["headroom"]) == 1
    assert all(r.alias == "dc1" and r.cloud == "onprem" for r in records["vm"])
    # stamped on one day, the day the detectors key on
    assert all(r.period == Period(start=DAY, end=DAY) for r in records["vm"])


def test_the_site_total_is_what_the_operator_pays(site_config):
    records = collect(site_config)
    total = sum(float(r.cost.amount) for r in records)
    # the pool costs the same whether it is full or empty, so the vms plus the headroom
    # line have to add back up to it
    assert total == pytest.approx(POOL_MONTHLY * DAY_SHARE)


def test_a_day_is_24_of_730_hours(site_config):
    records = by_service(collect(site_config))
    app = next(r for r in records["vm"] if r.resource.resource_id == "app-01")
    # 4 vcpu + 8 gib while it runs, plus the 60 gib it has written — not the 100 it may
    # grow into, which is nowhere on the operator's invoice
    monthly = 4 * 730 * 0.625 + 8 * 730 * 0.046875 + 60 * 1.46
    assert float(app.cost.amount) == pytest.approx(monthly * DAY_SHARE, abs=0.01)
    assert app.dimensions["disk_gib"] == "60"
    assert app.dimensions["disk_promised_gib"] == "100"
    # cents: the division runs to 28 digits and those end up in a slack message
    assert app.cost.amount == app.cost.amount.quantize(Decimal("0.01"))


def test_a_thin_promise_is_a_risk_and_never_spend(site_config):
    # every disk 2x thin, cpu and ram untouched: the pool must still bill its own card
    thin = {
        key: props | {"summary.storage.uncommitted": props["summary.storage.committed"]}
        for key, props in VMS.items()
    }
    records = collect(site_config, site_inventory(vms=thin))
    assert sum(float(r.cost.amount) for r in records) == pytest.approx(POOL_MONTHLY * DAY_SHARE)
    dims = by_service(records)["headroom"][0].dimensions
    # occupied is what is billed, promised is the number a capacity talk needs
    assert float(dims["overcommit_storage"]) == pytest.approx(160 / 1000)
    assert float(dims["overcommit_storage_promised"]) == pytest.approx(320 / 1000)


def test_two_vms_with_one_name_still_both_get_a_line(site_config):
    # vcenter allows it (different folders), and keying by name would lose a vm's spend
    twins = {
        key: props | {"name": "web-01"} for key, props in VMS.items()
    }
    records = by_service(collect(site_config, site_inventory(vms=twins)))
    assert len(records["vm"]) == 2
    assert {r.resource.resource_id for r in records["vm"]} == {
        "web-01 (vim.VirtualMachine:vm-10)",
        "web-01 (vim.VirtualMachine:vm-11)",
    }
    assert {r.dimensions["moref"] for r in records["vm"]} == set(twins)


def test_a_stopped_vm_is_charged_for_its_disk_only(site_config):
    records = by_service(collect(site_config))
    old = next(r for r in records["vm"] if r.resource.resource_id == "old-01")
    assert old.dimensions["state"] == "stopped"
    assert float(old.cost.amount) == pytest.approx(100 * 1.46 * DAY_SHARE, abs=0.01)


def test_dimensions_carry_the_cluster_and_the_host(site_config):
    records = by_service(collect(site_config))
    app = next(r for r in records["vm"] if r.resource.resource_id == "app-01")
    assert app.dimensions["cluster"] == "DC0/prod"
    assert app.dimensions["host"] == "esx-01"
    assert app.dimensions["vcpu"] == "4"
    assert app.resource.region == "DC0/prod"


def test_the_headroom_record_defends_its_own_numbers(site_config):
    headroom = by_service(collect(site_config))["headroom"][0]
    dims = headroom.dimensions
    assert dims["rate_vcpu_hour"] == "0.625"
    assert dims["rate_storage_gib_month"] == "1.46"
    # the weights are arguable, so they travel next to the rates they produced
    assert dims["weights"] == "cpu=0.5,ram=0.3,storage=0.2"
    assert dims["capacity_vcpu"] == "8"
    assert dims["hosts"] == "1"
    assert dims["vms"] == "2"
    assert float(dims["allocated_ratio"]) < 1


def test_an_overcommitted_pool_has_no_headroom_to_bill(site_config):
    # 2x the iron asked for on every axis: 16 vcpu of 8 cores, 128 of 64 gib, 2000 of 1000
    greedy = {
        f"vim.VirtualMachine:vm-{n}": {
            "name": f"fat-0{n}",
            "runtime.host": "vim.HostSystem:host-1",
            "runtime.powerState": "poweredOn",
            "config.hardware.numCPU": 8,
            "config.hardware.memoryMB": 64 * 1024,
            "summary.storage.committed": 1000 * GIB,
            "summary.storage.uncommitted": 0,
        }
        for n in (20, 21)
    }
    hosts = {"vim.HostSystem:host-1": HOSTS["vim.HostSystem:host-1"] | {"vm": list(greedy)}}
    records = by_service(collect(site_config, site_inventory(hosts=hosts, vms=greedy)))

    headroom = records["headroom"][0]
    assert headroom.cost.amount == Decimal(0)
    assert float(headroom.dimensions["allocated_ratio"]) == pytest.approx(2.0)
    # the money is clamped, the figure is not: -7300 is the overshoot, in the record
    assert float(headroom.dimensions["headroom_monthly"]) == pytest.approx(-POOL_MONTHLY)
    # and the vms are still charged in full - overcommit is reported, never clamped
    charged = sum(float(r.cost.amount) for r in records["vm"])
    assert charged == pytest.approx(2 * POOL_MONTHLY * DAY_SHARE)


def test_a_qualified_cluster_name_wins(site_config):
    # two datacenters may each hold a "prod", so DC0/prod is how they are told apart
    both = OnPremSite(
        **CARD,
        clusters={"prod": {"rate_card": {"hardware_amortization": 100}}, "DC0/prod": {}},
    )
    headroom = by_service(collect(both))["headroom"][0]
    assert headroom.dimensions["pool_monthly"] == "7300"


def test_a_cluster_nobody_priced_gets_the_site_card():
    records = by_service(collect(OnPremSite(**CARD)))
    assert records["headroom"][0].dimensions["pool_monthly"] == "7300"


def test_an_unpriceable_pool_is_skipped_not_fatal(site_config):
    # a second cluster whose host mounts no datastore: storage capacity 0, no rates
    hosts = HOSTS | {
        "vim.HostSystem:host-2": {
            "name": "esx-02",
            "hardware.cpuInfo.numCpuCores": 8,
            "hardware.memorySize": 64 * GIB,
            "runtime.powerState": "poweredOn",
        }
    }
    records = collect(site_config, site_inventory(hosts=hosts))
    assert {r.dimensions["cluster"] for r in records} == {"DC0/prod"}


def test_no_pool_priced_at_all_raises(site_config):
    hosts = {
        "vim.HostSystem:host-2": {
            "name": "esx-02",
            "hardware.cpuInfo.numCpuCores": 8,
            "hardware.memorySize": 64 * GIB,
            "runtime.powerState": "poweredOn",
        }
    }
    with pytest.raises(ConfigError, match="no pool could be priced"):
        collect(site_config, site_inventory(clusters={}, hosts=hosts, vms={}))


def test_recommendations_stay_with_the_waste_collector(site_config):
    provider = FakeProvider(site_config, site_inventory())
    assert OnPremCostCollector(provider).recommendations(PERIOD) == []


# the measured half: the perf pass splits a record, it never changes it

MEASURED = Usage(
    vcpu=Decimal(1),       # 25% of 4
    ram_gib=Decimal(4),    # 50% of 8
    cpu_pct=Decimal(25),
    ram_pct=Decimal(50),
    samples=48,
)


def measured_inventory():
    inv = site_inventory()
    inv.usage["vim.VirtualMachine:vm-10"] = MEASURED
    return inv


def test_a_measured_vm_is_still_billed_what_it_reserved(site_config):
    plain = by_service(collect(site_config))
    split = by_service(collect(site_config, measured_inventory()))
    # the invoice does not shrink because a vm ran quietly, so neither does the record
    assert [r.cost.amount for r in plain["vm"]] == [r.cost.amount for r in split["vm"]]
    assert plain["headroom"][0].cost.amount == split["headroom"][0].cost.amount


def test_the_used_and_wasted_split_rides_along_as_dimensions(site_config):
    records = by_service(collect(site_config, measured_inventory()))
    app = next(r for r in records["vm"] if r.resource.resource_id == "app-01")
    assert app.dimensions["measured"] == "yes"
    assert app.dimensions["used_vcpu"] == "1"
    assert app.dimensions["cpu_pct"] == "25"
    assert app.dimensions["samples"] == "48"
    # 1 vcpu + 4 gib + the same 60 gib of disk, against 4 vcpu + 8 gib + 60 gib
    assert float(app.dimensions["used_monthly"]) == pytest.approx(680.725, abs=0.01)
    assert float(app.dimensions["waste_monthly"]) == pytest.approx(1505.625, abs=0.01)


def test_a_vm_with_no_history_says_so_instead_of_claiming_zero(site_config):
    records = by_service(collect(site_config, measured_inventory()))
    old = next(r for r in records["vm"] if r.resource.resource_id == "old-01")
    assert old.dimensions["measured"] == "no"
    # a 0 here would read as a perfectly idle vm to every detector downstream
    assert not [key for key in old.dimensions if key.startswith("used_")]


def test_the_pool_line_says_how_much_of_it_was_measured(site_config):
    dims = by_service(collect(site_config, measured_inventory()))["headroom"][0].dimensions
    # "40% wasted" off one measured vm out of two is a lie with a number on it
    assert dims["measured_vms"] == "1"
    assert dims["vms"] == "2"
    assert float(dims["total_used_monthly"]) < float(dims["total_provisioned_monthly"])


# the provider: everything that does not need a vcenter on the other end

INVENTORY = {"endpoint": "vc1.dc1", "username": "clont-ro", "password": "s3cret"}


class FakeSession:
    def __init__(self, site, counter, windows):
        self._site = site
        self._counter = counter
        self._windows = windows
        self.instance_uuid = "uuid-1"

    def __enter__(self):
        self._counter.append(1)
        return self

    def __exit__(self, *_exc):
        return None

    def site(self, *, usage_window_days=0):
        self._windows.append(usage_window_days)
        return self._site


def provider_with(session_site, clock_value=None, inventory=None, **site_kwargs):
    logins: list[int] = []
    windows: list[int] = []
    now = [0.0] if clock_value is None else clock_value
    site = OnPremSite(inventory=inventory or INVENTORY, **(site_kwargs or CARD))
    provider = OnPremProvider("dc1", site, clock=lambda: now[0])
    provider._session = lambda: FakeSession(session_site, logins, windows)
    provider.windows = windows
    return provider, logins, now


def test_a_pass_is_reused_inside_the_ttl():
    provider, logins, now = provider_with(site_inventory())
    provider.inventory()
    provider.inventory()
    assert len(logins) == 1
    # vcenter expires an idle session, so the result is cached and the login is not
    now[0] = 10_000
    provider.inventory()
    assert len(logins) == 2


def test_refresh_ignores_the_cache():
    provider, logins, _ = provider_with(site_inventory())
    provider.inventory()
    provider.inventory(refresh=True)
    assert len(logins) == 2


def test_regions_are_the_pools():
    provider, _, _ = provider_with(site_inventory())
    assert provider.regions() == ["DC0/prod"]


def test_authenticate_records_the_vcenter_id():
    provider, _, _ = provider_with(site_inventory())
    provider.authenticate()
    assert provider.account_id == "uuid-1"


def test_preflight_names_a_login_that_sees_nothing():
    provider, _, _ = provider_with(site_inventory(clusters={}, hosts={}, datastores={}, vms={}))
    assert provider.preflight() == ["clont-ro: no cluster or host is visible"]


def test_preflight_is_quiet_when_the_role_is_right():
    provider, _, _ = provider_with(site_inventory())
    assert provider.preflight() == []


def test_the_perf_window_rides_on_the_same_pass():
    provider, logins, _ = provider_with(site_inventory())
    provider.inventory()
    # one login for the inventory and the counters both: the perf read is the expensive
    # half, and paying for it in a second pass would walk the whole vcenter twice
    assert len(logins) == 1
    assert provider.windows == [14]


def test_a_zero_window_reads_no_counters_at_all():
    provider, _, _ = provider_with(site_inventory(), inventory=INVENTORY | {"usage_window_days": 0})
    provider.inventory()
    assert provider.windows == [0]


def test_the_window_cannot_outrun_what_vcenter_keeps():
    # 2-hour rollups live 30 days by default, so a longer ask would silently read short
    with pytest.raises(ValueError, match="usage_window_days"):
        InventoryConfig(endpoint="vc1", username="ro", password="x", usage_window_days=60)


def test_a_site_with_no_inventory_block_is_not_a_provider():
    with pytest.raises(ConfigError, match="no inventory block"):
        OnPremProvider("dc1", OnPremSite(**CARD))


def test_exactly_one_password_source():
    with pytest.raises(ValueError, match="exactly one"):
        InventoryConfig(endpoint="vc1", username="ro")
    with pytest.raises(ValueError, match="exactly one"):
        InventoryConfig(endpoint="vc1", username="ro", password="x", password_env="VC_PW")


def test_the_password_env_is_read_at_use_time(monkeypatch):
    config = InventoryConfig(endpoint="vc1", username="ro", password_env="VC_PW")
    monkeypatch.delenv("VC_PW", raising=False)
    with pytest.raises(ConfigError, match="VC_PW"):
        config.secret()
    monkeypatch.setenv("VC_PW", "later")
    assert config.secret() == "later"
