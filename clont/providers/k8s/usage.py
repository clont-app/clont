"""What a workload actually ran at, as opposed to what its template asked for.

`pods.py` is the request column; this is the other one, and it is what turns the namespace
table into sizing advice at all. Same split as the on-prem side (`onprem/inventory.py` vs
`onprem/metrics.py`) and the percentile is literally the same function — `core/stats.py`
is nearest-rank over plain numbers and knows nothing about either provider.

**Two sources, and they are not equally good:**

| source | what one read gives | how a p95 is reached |
|---|---|---|
| metrics-server | *one instant sample* per container, ~30s wide, no history at all | clont keeps the samples itself, one per pass, in `UsageHistory` |
| prometheus | a p95 over the real window, server-side | one query, already an answer |

So **metrics-server cannot answer "p95 over two weeks" and is not asked to.** It is a
liveness gauge: a single sample is not evidence to shrink anything, and a tool that shrank
a workload off one reading would be advising on whatever happened in the last 30 seconds.
The ring makes it honest — until `usage_min_samples` have accumulated the workload simply
has no row, the same rule layer 3 pinned for a vm with too little history. The history
lives in the process, so a restart starts the clock again; that is the price of the cheap
source and the reason prometheus exists in the config.

Three more decisions:

* **the sample is taken per workload, not per pod.** A rollout replaces every pod name, so
  a per-pod ring forgets everything exactly when the operator is most likely to look. Usage
  does not depend on what was requested, so the history survives a template change and is
  still the right evidence.
* **the workload's number is the busiest replica's**, maxed per dimension. The template is
  shrunk for all of them at once, so the limit is the hottest one — an average replica would
  size the template to under what the hot one already uses.
* **enough history on one replica is enough history.** The sample count is the max over the
  replicas, because a scale-out that added a fresh pod this minute has not made the
  workload's two weeks of measurement younger.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from clont.core.stats import DEFAULT_QUANTILE, percentile
from clont.providers.k8s.nodes import BYTES_PER_GIB, quantity
from clont.providers.k8s.pods import Pod, WorkloadRef

METRICS_SERVER = "metrics-server"
PROMETHEUS = "prometheus"
USAGE_OFF = "off"

# 24h of samples at a 5-minute pass. A longer ring is thousands of Decimals per workload
# for a percentile that barely moves; a shorter one cannot see a daily peak at all
DEFAULT_RING = 288


@dataclass(frozen=True, slots=True)
class PodUsage:
    """One pod's measured consumption: an instant sample, or a p95 prometheus computed."""

    namespace: str
    name: str
    vcpu: Decimal = Decimal(0)
    ram_gib: Decimal = Decimal(0)
    samples: int = 1  # how many points are behind it — 1 for a metrics-server reading


@dataclass(frozen=True, slots=True)
class WorkloadUsage:
    """One workload's usage, as the sizing pass reads it."""

    vcpu: Decimal
    ram_gib: Decimal
    samples: int
    replicas: int
    source: str


def build_usage(items: Iterable[dict]) -> list[PodUsage]:
    """metrics-server's `PodMetricsList.items` as clont sees it.

    A pod's usage is the sum over its containers, which is the same shape as its request —
    per-container advice is out of scope, so the two columns have to be comparable.
    """
    out: list[PodUsage] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        meta = _sub(item, "metadata")
        name = str(meta.get("name") or "").strip()
        if not name:
            continue
        vcpu = ram = Decimal(0)
        for container in _list(item.get("containers")):
            used = _sub(container, "usage")
            vcpu += quantity(used.get("cpu"))
            ram += quantity(used.get("memory")) / BYTES_PER_GIB
        out.append(
            PodUsage(
                namespace=str(meta.get("namespace") or "").strip(),
                name=name,
                vcpu=vcpu,
                ram_gib=ram,
            )
        )
    return out


def fold(
    pods: Iterable[Pod],
    usage: Iterable[PodUsage],
    *,
    source: str,
    min_samples: int = 1,
) -> dict[WorkloadRef, WorkloadUsage]:
    """Per-pod rows that are already percentiles, folded onto their workloads.

    This is the prometheus path: the server did the window, so there is nothing to
    accumulate. A pod the cluster does not list is dropped — it is history prometheus still
    remembers, and a deleted replica cannot size a live template.
    """
    rows = {(row.namespace, row.name): row for row in usage}
    out: dict[WorkloadRef, WorkloadUsage] = {}
    for key, group in _by_workload(pods).items():
        found = [rows[(pod.namespace, pod.name)] for pod in group if (pod.namespace, pod.name) in rows]
        if not found:
            continue
        taken = WorkloadUsage(
            vcpu=max(row.vcpu for row in found),
            ram_gib=max(row.ram_gib for row in found),
            samples=max(row.samples for row in found),
            replicas=len(group),
            source=source,
        )
        if taken.samples >= min_samples:
            out[key] = taken
    return out


class UsageHistory:
    """The samples metrics-server does not keep, kept per workload.

    One `observe()` per cluster pass, one sample per workload: the max over its replicas.
    A workload that stops appearing is forgotten, so a cluster that churns namespaces does
    not grow the ring forever.
    """

    def __init__(self, ring: int = DEFAULT_RING) -> None:
        self._ring = ring
        self._cpu: dict[WorkloadRef, deque[Decimal]] = {}
        self._ram: dict[WorkloadRef, deque[Decimal]] = {}

    def observe(self, pods: Iterable[Pod], usage: Iterable[PodUsage]) -> None:
        rows = {(row.namespace, row.name): row for row in usage}
        live = _by_workload(pods)
        for key, group in live.items():
            found = [
                rows[(pod.namespace, pod.name)]
                for pod in group
                if (pod.namespace, pod.name) in rows
            ]
            if not found:
                continue
            self._push(self._cpu, key, max(row.vcpu for row in found))
            self._push(self._ram, key, max(row.ram_gib for row in found))
        self._forget(set(live))

    def rows(
        self,
        pods: Iterable[Pod],
        *,
        min_samples: int,
        quantile: Decimal = DEFAULT_QUANTILE,
    ) -> dict[WorkloadRef, WorkloadUsage]:
        """The workloads with enough history to be advised about, and nobody else."""
        out: dict[WorkloadRef, WorkloadUsage] = {}
        for key, group in _by_workload(pods).items():
            cpu = self._cpu.get(key) or deque()
            ram = self._ram.get(key) or deque()
            count = min(len(cpu), len(ram))
            if count < max(1, min_samples):
                continue
            out[key] = WorkloadUsage(
                vcpu=percentile(list(cpu), quantile),
                ram_gib=percentile(list(ram), quantile),
                samples=count,
                replicas=len(group),
                source=METRICS_SERVER,
            )
        return out

    def _push(self, store: dict[WorkloadRef, deque[Decimal]], key: WorkloadRef, value: Decimal) -> None:
        series = store.get(key)
        if series is None:
            series = store[key] = deque(maxlen=self._ring)
        series.append(value)

    def _forget(self, live: set[WorkloadRef]) -> None:
        for gone in [key for key in self._cpu if key not in live]:
            self._cpu.pop(gone, None)
            self._ram.pop(gone, None)


def _by_workload(pods: Iterable[Pod]) -> dict[WorkloadRef, list[Pod]]:
    out: dict[WorkloadRef, list[Pod]] = {}
    for pod in pods:
        out.setdefault(pod.workload, []).append(pod)
    return out


def _list(value: object) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _sub(item: dict, key: str) -> dict:
    value = item.get(key)
    return value if isinstance(value, dict) else {}
