"""Measured usage: vcenter's own perf counters turned into "vcpu used" and "gib used".

`inventory.py` measures what a vm was *asked for*; this is the other column — what it
actually ran at — and it is what turns the cost records into a waste report and makes
idle/rightsizing findings possible at all.

Four decisions, all of them the arguable kind:

* **percent counters, not `usagemhz` / `mem.active` in absolute units.** `cpu.usage.average`
  on a vm is a share of *its own* configured capacity, so a vcpu figure is `vcpu * pct`
  and needs nothing about the host it happens to sit on. The MHz counter would have to be
  divided by that host's hz-per-core, which is a second property call and a wrong answer
  the day a vm vmotions between two hardware generations mid-window
* **p95, nearest-rank, over the trailing window.** A mean hides the peak that sizing
  exists for and an average of averages is not a percentile; nearest-rank needs no
  interpolation and answers the same on 8 samples as on 8000. The samples are already
  interval *averages* (2-hour rollups by default), so the p95 is a p95 of smoothed data —
  it is a floor for the real peak, never above it
* **a usage row is clamped to what the vm owns.** vcenter accounts a vm's own overhead
  into these counters, so 103% of 4 vcpu comes back on a busy host; charging 4.12 vcpu
  would report waste as a negative number
* **too few samples means no row at all.** A vm created yesterday has 12 samples in a
  14-day window, and a p95 of that is a guess with a decimal point on it. With no row
  `allocate()` charges provisioned and the waste column stays empty, which is the honest
  shape — see `rates.allocate()`.

Keeping this module free of pyvmomi (and of vsphere's own vocabulary past the counter
names) is what lets the libvirt/proxmox pass reuse it: fill the same
`{vm moref: {counter: [values]}}` dict and the arithmetic is done.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from clont.core.stats import DEFAULT_QUANTILE, percentile
from clont.providers.onprem.inventory import Vm

# a vm's share of its own configured capacity, in hundredths of a percent (1234 = 12.34%)
CPU_PCT = "cpu.usage.average"
RAM_PCT = "mem.usage.average"
COUNTERS = (CPU_PCT, RAM_PCT)

HUNDREDTHS = Decimal(10000)
# 2-hour rollups, so 24 samples is two days of history
MIN_SAMPLES = 24

# {vm moref -> {counter name -> samples}}, what the perf manager gives per entity
Samples = dict[str, dict[str, list[float]]]


@dataclass(frozen=True, slots=True)
class Usage:
    """One vm's measured consumption over the window. `vcpu`/`ram_gib` are p95."""

    vcpu: Decimal
    ram_gib: Decimal
    cpu_pct: Decimal   # of its own configured vcpu, 0-100
    ram_pct: Decimal   # of its own configured ram, 0-100
    samples: int

    def used(self, committed_gib: Decimal) -> dict[str, Decimal]:
        """This vm's `used` row for `rates.allocate()`.

        Storage is the space it occupies and nothing else: a guest's free space inside
        its own filesystem is still written blocks on the array, and clont cannot see
        into the guest anyway.
        """
        return {"vcpu": self.vcpu, "ram_gib": self.ram_gib, "disk_gib": committed_gib}


def usage_rows(
    vms: Iterable[Vm],
    samples: Samples,
    *,
    min_samples: int = MIN_SAMPLES,
    quantile: Decimal = DEFAULT_QUANTILE,
) -> dict[str, Usage]:
    """moref -> measured usage, for the vms that have enough history to answer.

    A powered-off vm is skipped: its counters are zeros for the whole window, and calling
    that "0% used" would make every stopped vm the biggest idle finding on the floor.
    """
    rows: dict[str, Usage] = {}
    for vm in vms:
        if not vm.running:
            continue
        series = samples.get(vm.uid) or {}
        cpu = _series(series.get(CPU_PCT))
        ram = _series(series.get(RAM_PCT))
        count = min(len(cpu), len(ram))
        if count < min_samples:
            continue
        cpu_pct = _clamp_pct(percentile(cpu, quantile))
        ram_pct = _clamp_pct(percentile(ram, quantile))
        rows[vm.uid] = Usage(
            vcpu=Decimal(vm.vcpu) * cpu_pct / 100,
            ram_gib=vm.ram_gib * ram_pct / 100,
            cpu_pct=cpu_pct,
            ram_pct=ram_pct,
            samples=count,
        )
    return rows


def _series(raw: object) -> list[Decimal]:
    # a counter that was never collected is absent, and -1 is vcenter's "no data" sample
    if not isinstance(raw, list | tuple):
        return []
    out = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int | float | str | Decimal):
            continue
        value = Decimal(str(item))
        if value.is_finite() and value >= 0:
            out.append(value / HUNDREDTHS * 100)
    return out


def _clamp_pct(value: Decimal) -> Decimal:
    # a vm's own virtualization overhead is charged to it, so >100% is normal and real
    return min(max(value, Decimal(0)), Decimal(100))
