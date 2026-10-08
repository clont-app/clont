"""Requests against measured usage, per workload: `rightsize-workload`.

The third piece of the plan's k8s step, and deliberately the same arithmetic as
`onprem/waste.py`'s `rightsize-vm` — same `onprem_rightsize_target_pct` knob, same "leave
the p95 at that share of the new size", same "no measurement means no row". A pod template
is a vm's `vcpu` / `ram_gib` in another spelling, so a second set of rules would just be a
second set of numbers to reconcile.

    pool card -> vm CostRecord -> node -> $/vcpu-month -> what this template hands back

**What is being offered back is capacity, not cash, and the report says so.** Shrinking a
request moves money from a namespace to the `(unrequested)` bucket; the invoice only moves
when the freed capacity lets the cluster drop a node. So the finding is `approximate` and
its summary names the pool — it is the strongest honest claim, and a tool that quietly
added these up into "savings" would be promising an operator money that is still in the
rate card.

Decisions worth keeping:

* **the target comes off the busiest replica's p95, never the average.** One template sizes
  every replica, so an average would advise a number the hot pod already exceeds.
* **each replica is priced on the node it actually sits on.** Two node pools in one cluster
  have two rate cards, and a workload spread over both hands back different money per pod.
  A replica on an unpriced node contributes nothing and is counted, same rule as the
  namespace split.
* **a dimension that asks for nothing is left alone.** A pod with no cpu request cannot
  hand cpu back, and advising a *new* request here would be inventing a number the
  scheduler never used — that is a different finding, not this one.
* **a request below the measured p95 of ram is its own finding at $0.** cpu above its
  request is normal and compressible: that is what a request *is*. Memory is not — working
  set over the request is the pod first in line when the node needs memory back, so
  `underrequested-workload` is a risk line, priced at zero like `thin-overcommit`.
* **the advised number is rounded to something a human would type**, 10m of cpu and 16Mi of
  ram, always upward. A yaml patch of `cpu: 137m` reads as machine output and gets ignored.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from clont.core.logging import get_logger
from clont.finops.base import FinOpsTuning
from clont.finops.k8s.mapping import ClusterMapping
from clont.finops.k8s.prices import NodeRates, Prices
from clont.finops.models import CostRecord
from clont.providers.k8s.pods import Pod, WorkloadRef
from clont.providers.k8s.usage import WorkloadUsage

log = get_logger("clont.finops.k8s.workloads")

RIGHTSIZE = "rightsize-workload"
UNDERREQUESTED = "underrequested-workload"

# what a request is rounded to: 10 millicores, 16 MiB. a human edits these by hand
CPU_STEP = Decimal("0.01")
RAM_STEP_GIB = Decimal(16) / Decimal(1024)

_CENT = Decimal("0.01")
_HUNDRED = Decimal(100)
_USD = "USD"


@dataclass(frozen=True, slots=True)
class WorkloadFinding:
    """One pod template to resize, priced per month."""

    kind: str
    ref: str                 # namespace/kind/name, as an operator finds it with kubectl
    region: str              # the pool that prices the nodes it runs on
    summary: str
    monthly: Decimal         # usd/month handed back to the pool, 0 for a risk
    currency: str = _USD
    replicas: int = 0


@dataclass(frozen=True, slots=True)
class WorkloadReport:
    """One cluster's sizing pass: what it advised, and what it could not see."""

    cluster: str
    source: str = ""                                  # metrics-server | prometheus | off
    findings: tuple[WorkloadFinding, ...] = ()        # biggest first
    measured: int = 0        # workloads with enough history to be advised about
    unmeasured: int = 0      # live workloads with no usable usage row
    norequest: int = 0       # ...and workloads that reserve nothing, so nothing to shrink
    unpriced_replicas: int = 0

    def summary(self) -> str:
        head = (
            f"{self.cluster}: {len(self.findings)} sizing finding(s) over {self.measured} "
            f"measured workload(s) via {self.source or 'no usage source'}"
        )
        if self.unmeasured:
            head += f", {self.unmeasured} not measured yet"
        if self.norequest:
            head += f", {self.norequest} requesting nothing"
        if self.unpriced_replicas:
            head += f", {self.unpriced_replicas} replica(s) on unpriced nodes"
        return head


def advise(
    mapping: ClusterMapping,
    pods: Iterable[Pod],
    usage: dict[WorkloadRef, WorkloadUsage],
    records: list[CostRecord],
    *,
    tuning: FinOpsTuning | None = None,
    source: str = "",
) -> WorkloadReport:
    """Every workload whose template asks for more than it has ever used."""
    tune = tuning or FinOpsTuning()
    target = Decimal(str(tune.onprem_rightsize_target_pct)) / _HUNDRED
    floor = Decimal(str(tune.onprem_min_savings_usd))
    rates = _rates_by_node(mapping, records)
    pool = ", ".join(mapping.pools) or "unmapped"

    findings: list[WorkloadFinding] = []
    measured = unmeasured = norequest = unpriced = 0
    for key, group in _by_workload(pods).items():
        row = usage.get(key)
        if row is None:
            unmeasured += 1
            continue
        measured += 1
        ask_cpu = max((pod.vcpu for pod in group), default=Decimal(0))
        ask_ram = max((pod.ram_gib for pod in group), default=Decimal(0))
        unpriced += sum(1 for pod in group if rates.get(pod.node) is None)
        if ask_cpu <= 0 and ask_ram <= 0:
            norequest += 1
            continue
        if ask_ram > 0 and row.ram_gib > ask_ram:
            findings.append(_underrequested(key, pool, group, row, ask_ram))
            continue
        fit = _fit(ask_cpu, ask_ram, row, target)
        if fit is None:
            continue
        cpu, ram = fit
        monthly, currency = _handback(group, rates, cpu, ram)
        if monthly < floor:
            continue
        findings.append(
            WorkloadFinding(
                kind=RIGHTSIZE,
                ref=key.ref,
                region=pool,
                summary=_summary(key, group, row, (ask_cpu, ask_ram), (cpu, ram), tune),
                monthly=_money(monthly),
                currency=currency,
                replicas=len(group),
            )
        )
    report = WorkloadReport(
        cluster=mapping.cluster,
        source=source,
        findings=tuple(sorted(findings, key=lambda f: (-f.monthly, f.ref))),
        measured=measured,
        unmeasured=unmeasured,
        norequest=norequest,
        unpriced_replicas=unpriced,
    )
    log.debug("%s", report.summary())
    return report


def _fit(
    ask_cpu: Decimal, ask_ram: Decimal, row: WorkloadUsage, target: Decimal
) -> tuple[Decimal, Decimal] | None:
    """The smallest human-shaped request that leaves the p95 at `target` of it.

    Never above what the template already asks for — this pass only ever shrinks — and
    None when neither dimension moves, which is the gate rather than a second threshold.
    """
    cpu = min(ask_cpu, _step_up(row.vcpu / target, CPU_STEP)) if ask_cpu > 0 else ask_cpu
    ram = min(ask_ram, _step_up(row.ram_gib / target, RAM_STEP_GIB)) if ask_ram > 0 else ask_ram
    if cpu >= ask_cpu and ram >= ask_ram:
        return None
    return cpu, ram


def _handback(
    pods: list[Pod], rates: dict[str, NodeRates | None], cpu: Decimal, ram: Decimal
) -> tuple[Decimal, str]:
    """What the replicas give back, each one at its own node's rate.

    Per pod, not per template: a rollout has two templates alive at once, and a replica
    that already asks for less than the advised size hands back nothing rather than a
    negative number.
    """
    total = Decimal(0)
    currency = _USD
    for pod in pods:
        rate = rates.get(pod.node)
        if rate is None:
            continue
        currency = rate.currency or currency
        total += max(pod.vcpu - cpu, Decimal(0)) * rate.per_vcpu
        total += max(pod.ram_gib - ram, Decimal(0)) * rate.per_gib
    return total, currency


def _underrequested(
    key: WorkloadRef, pool: str, pods: list[Pod], row: WorkloadUsage, ask_ram: Decimal
) -> WorkloadFinding:
    return WorkloadFinding(
        kind=UNDERREQUESTED,
        ref=key.ref,
        region=pool,
        summary=(
            f"{key.ref} requests {_mib(ask_ram)} Mi of memory and its busiest replica's "
            f"p95 working set is {_mib(row.ram_gib)} Mi ({row.samples} samples, "
            f"{row.source}) — it is holding memory it never reserved, so it is first to be "
            f"evicted when the node needs it back. this is a risk, not a saving"
        ),
        monthly=Decimal(0),
        replicas=len(pods),
    )


def _summary(
    key: WorkloadRef,
    pods: list[Pod],
    row: WorkloadUsage,
    ask: tuple[Decimal, Decimal],
    fit: tuple[Decimal, Decimal],
    tune: FinOpsTuning,
) -> str:
    return (
        f"{key.ref} ({len(pods)} replica(s)) peaked at {_milli(row.vcpu)}m cpu and "
        f"{_mib(row.ram_gib)} Mi (p95 over {row.samples} samples, {row.source}) on "
        f"{_milli(ask[0])}m / {_mib(ask[1])} Mi requested — "
        f"{_milli(fit[0])}m / {_mib(fit[1])} Mi leaves the peak at "
        f"{tune.onprem_rightsize_target_pct:.0f}% of the new request. the capacity returns "
        f"to the pool's unrequested share; it turns into money when a node can go"
    )


def _rates_by_node(
    mapping: ClusterMapping, records: list[CostRecord]
) -> dict[str, NodeRates | None]:
    """node name -> its monthly per-unit rates. Both spellings, as the split does."""
    prices = Prices(records)
    out: dict[str, NodeRates | None] = {}
    for match in mapping.matched:
        rate = prices.rates(match.target, match.node)
        for name in (match.node.name, match.node.short_name):
            if name:
                out[name] = rate
    for miss in mapping.unmapped:
        for name in (miss.node.name, miss.node.short_name):
            if name:
                out.setdefault(name, None)
    return out


def _by_workload(pods: Iterable[Pod]) -> dict[WorkloadRef, list[Pod]]:
    out: dict[WorkloadRef, list[Pod]] = {}
    for pod in pods:
        out.setdefault(pod.workload, []).append(pod)
    return out


def _step_up(value: Decimal, step: Decimal) -> Decimal:
    """Up to the next whole `step`, and never to zero: a request of 0 is not a size."""
    steps = (value / step).to_integral_value(rounding=ROUND_CEILING)
    return max(Decimal(1), steps) * step


def _money(amount: Decimal) -> Decimal:
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


def _milli(vcpu: Decimal) -> str:
    return f"{float(vcpu) * 1000:,.0f}"


def _mib(gib: Decimal) -> str:
    return f"{float(gib) * 1024:,.0f}"
