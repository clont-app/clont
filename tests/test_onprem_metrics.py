"""Measured usage: perf-counter samples in, a p95 usage row out.

No vcenter here — the module takes a plain `{moref: {counter: [values]}}` dict on purpose,
so what is tested is the arithmetic and the four rules that decide whether a vm gets a row
at all. The wire that fills the dict is `vsphere.py`, covered by the image functests.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from clont.providers.onprem.inventory import Vm
from clont.providers.onprem.metrics import (
    CPU_PCT,
    RAM_PCT,
    Usage,
    percentile,
    usage_rows,
)


def vm(uid="vim.VirtualMachine:vm-1", *, vcpu=4, ram_gib=16, powered_on=True, template=False):
    return Vm(
        uid=uid,
        name="app-01",
        host="esx-01",
        powered_on=powered_on,
        template=template,
        vcpu=vcpu,
        ram_gib=Decimal(ram_gib),
        disk_gib=Decimal(100),
        committed_gib=Decimal(60),
    )


def flat(cpu_pct, ram_pct, count=48):
    """One vm's series, both counters flat, in vcenter's hundredths of a percent."""
    return {CPU_PCT: [cpu_pct * 100] * count, RAM_PCT: [ram_pct * 100] * count}


def test_a_percent_counter_becomes_vcpu_and_gib():
    rows = usage_rows([vm()], {"vim.VirtualMachine:vm-1": flat(25, 50)})
    row = rows["vim.VirtualMachine:vm-1"]
    # the counter is a share of the vm's own configured size, so no host property is needed
    assert row.vcpu == Decimal(1)
    assert row.ram_gib == Decimal(8)
    assert row.cpu_pct == Decimal(25)
    assert row.samples == 48


def test_the_peak_is_the_p95_not_the_mean():
    # 94 samples at 10%, 6 at 90%: a mean says 14.8%, the p95 says 90%
    series = {CPU_PCT: [1000] * 94 + [9000] * 6, RAM_PCT: [1000] * 94 + [9000] * 6}
    rows = usage_rows([vm()], {"vim.VirtualMachine:vm-1": series})
    assert rows["vim.VirtualMachine:vm-1"].cpu_pct == Decimal(90)


def test_nearest_rank_answers_on_a_handful_of_samples():
    values = [Decimal(n) for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)]
    assert percentile(values) == Decimal(10)
    assert percentile(values, Decimal("0.5")) == Decimal(5)
    # never interpolates, so the answer is always a sample the counter reported
    assert percentile([Decimal(7)]) == Decimal(7)
    assert percentile([]) == Decimal(0)


def test_a_vm_is_never_charged_more_than_it_owns():
    # vcenter accounts a vm's own overhead into the counter, so 103% comes back for real
    rows = usage_rows([vm(vcpu=4)], {"vim.VirtualMachine:vm-1": flat(103, 101)})
    row = rows["vim.VirtualMachine:vm-1"]
    assert row.vcpu == Decimal(4)
    assert row.ram_gib == Decimal(16)


def test_too_little_history_means_no_row_at_all():
    # 12 samples of 2-hour rollups is a day: a p95 of that is a guess with a decimal point
    rows = usage_rows([vm()], {"vim.VirtualMachine:vm-1": flat(50, 50, count=12)})
    assert rows == {}
    assert usage_rows([vm()], {}) == {}


def test_a_powered_off_vm_is_skipped_rather_than_called_idle():
    samples = {"vim.VirtualMachine:vm-1": flat(0, 0)}
    assert usage_rows([vm(powered_on=False)], samples) == {}
    assert usage_rows([vm(template=True)], samples) == {}


def test_vcenters_no_data_sample_is_dropped_not_read_as_zero():
    # -1 is "not collected"; counting it as 0% would drag every p95 down
    series = {CPU_PCT: [-1] * 40 + [5000] * 30, RAM_PCT: [5000] * 70}
    rows = usage_rows([vm()], {"vim.VirtualMachine:vm-1": series})
    row = rows["vim.VirtualMachine:vm-1"]
    assert row.cpu_pct == Decimal(50)
    # the shorter of the two series is what the row can honestly claim
    assert row.samples == 30


def test_one_missing_counter_drops_the_vm():
    rows = usage_rows([vm()], {"vim.VirtualMachine:vm-1": {CPU_PCT: [5000] * 48}})
    assert rows == {}


def test_storage_is_what_it_occupies_and_the_guest_is_not_visible():
    row = Usage(
        vcpu=Decimal(1),
        ram_gib=Decimal(8),
        cpu_pct=Decimal(25),
        ram_pct=Decimal(50),
        samples=48,
    )
    assert row.used(Decimal(60)) == {
        "vcpu": Decimal(1),
        "ram_gib": Decimal(8),
        "disk_gib": Decimal(60),
    }


def test_a_measured_vm_carries_used_into_the_allocator_and_the_rest_do_not():
    from clont.providers.onprem.inventory import Host, Pool

    pool = Pool(
        name="prod",
        kind="cluster",
        datacenter=None,
        hosts=(Host(name="esx-01", cores=8, threads=16, ram_gib=Decimal(64), powered_on=True),),
        datastores=(),
        vms=(vm(), vm("vim.VirtualMachine:vm-2")),
    )
    rows = usage_rows([vm()], {"vim.VirtualMachine:vm-1": flat(25, 50)})
    payload = pool.allocation_payload({"rate_card": {"hardware_amortization": 1000}}, rows)
    by_name = {entry["name"]: entry for entry in payload["vms"]}
    assert Decimal(by_name["vim.VirtualMachine:vm-1"]["used"]["vcpu"]) == Decimal(1)
    # no measurement is not zero usage: allocate() then charges what it reserved
    assert "used" not in by_name["vim.VirtualMachine:vm-2"]


@pytest.mark.parametrize("junk", [None, "x", [None], [True], ["nan"], [float("inf")]])
def test_a_junk_sample_cannot_reach_the_arithmetic(junk):
    series = {CPU_PCT: junk, RAM_PCT: junk}
    assert usage_rows([vm()], {"vim.VirtualMachine:vm-1": series}) == {}
