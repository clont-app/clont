"""Namespace showback: the node's own cost, divided by what each namespace asked for.

This is the second half of the plan's k8s step and it invents no price at all. `mapping.py`
said which priced thing a node runs on; the provider already emitted that thing's
`CostRecord` off the operator's rate card; this divides *that* number. Node vm -> cluster
pool -> namespace, which is the line between clont and a pod-metering tool: they multiply a
guessed node price, we split a measured one.

    pool card -> vm CostRecord -> node -> namespace requests

**Nothing is redistributed, and nothing is invented.** The split has four buckets and they
add back up to the nodes' cost:

| bucket | what it is |
|---|---|
| a namespace | its requests' share of the node's **capacity** |
| `(kubelet)` | `capacity - allocatable`: the reservation nobody can schedule into |
| `(unrequested)` | schedulable iron no pod asked for — the k8s twin of the on-prem `headroom` line |
| `(node-storage)` | whatever the pool card put on storage; no pod requests a vm's disk |

Decisions worth keeping:

* **shares are of capacity, not allocatable.** Dividing by allocatable would quietly spread
  the kubelet's reservation across every namespace as a markup nobody can see or act on.
  As its own line it is a number an operator can shrink.
* **requests, not usage.** The money is reservation-shaped: the vm was paid for whether the
  pod used it or not, and the scheduler handed out capacity on requests alone.
* **the cpu/ram weights come from the operator's own card**, read off the pool's
  `CostRecord` dimensions — the same `weights` that priced the vm. Guessing 50/50 when the
  card says `cpu=0.5,ram=0.3,storage=0.2` would move money between namespaces for no
  reason, so the card wins and the fallback only applies when no pool line is in the batch.
* **a node with no cost record is reported, never priced at zero.** On aws that is the
  normal case today: CUR records are per-service daily aggregates, so an eks node has no
  line of its own — the cluster comes back `unpriced` with the node names, and closing it
  means resource-level CUR, not a price guess here.
* **a namespace that only runs on unpriced nodes still appears**, at `0.00`, with its
  requests — an absent row would read as a namespace that costs nothing.

Reading the records is `prices.py`, shared with `workloads.py`: both halves divide the same
numbers and neither is allowed its own idea of what a node costs.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from clont.finops.k8s.mapping import ClusterMapping
from clont.finops.k8s.prices import Prices
from clont.finops.models import CostRecord
from clont.finops.showback import UNATTRIBUTED, ShowbackLine
from clont.providers.k8s.pods import Pod

KUBELET = "(kubelet)"
UNREQUESTED = "(unrequested)"
NODE_STORAGE = "(node-storage)"

_CENT = Decimal("0.01")
_TENTH = Decimal("0.1")
_USD = "USD"


@dataclass(frozen=True, slots=True)
class NamespaceLine:
    """One namespace's share of the iron its pods sit on."""

    namespace: str
    amount: Decimal
    share_pct: Decimal
    vcpu: Decimal           # requested, summed over its live pods
    ram_gib: Decimal
    pods: int
    labels: dict[str, str] = field(default_factory=dict)
    # nodes it runs on that carry no cost record, so this amount is partial
    unpriced_nodes: int = 0


@dataclass(frozen=True, slots=True)
class NamespaceShowback:
    """One cluster's spend split by namespace, plus the three buckets that are not one."""

    cluster: str
    currency: str
    start: date
    end: date
    total: Decimal                       # the priced nodes' cost, all buckets included
    lines: tuple[NamespaceLine, ...] = ()  # biggest first
    kubelet: Decimal = Decimal(0)
    unrequested: Decimal = Decimal(0)
    node_storage: Decimal = Decimal(0)
    nodes_priced: int = 0
    unpriced_nodes: tuple[str, ...] = ()   # mapped to iron, but no cost record for it
    unmapped_nodes: tuple[str, ...] = ()   # mapping.py could not place them at all
    pending_pods: int = 0                  # on no node, so holding nothing
    overrequested_nodes: tuple[str, ...] = ()

    @property
    def requested(self) -> Decimal:
        return _money(sum((line.amount for line in self.lines), Decimal(0)))

    @property
    def unrequested_pct(self) -> Decimal:
        return _pct(self.unrequested, self.total)

    def buckets(self) -> tuple[tuple[str, Decimal], ...]:
        """The three lines that are not a namespace, named as a table prints them."""
        return (
            (KUBELET, self.kubelet),
            (UNREQUESTED, self.unrequested),
            (NODE_STORAGE, self.node_storage),
        )

    @property
    def priced(self) -> bool:
        """Whether any of it is priced at all — an all-unpriced cluster reports no table."""
        return self.nodes_priced > 0

    def by_label(self, key: str) -> tuple[ShowbackLine, ...]:
        """The same money grouped by a namespace label, e.g. `team`.

        Namespaces with no value for the key land in `(untagged)` — the same constant the
        aws tag showback uses, because it is the same question and the same table.
        """
        totals: dict[str, Decimal] = defaultdict(Decimal)
        for line in self.lines:
            totals[(line.labels.get(key) or "").strip() or UNATTRIBUTED] += line.amount
        total = sum(totals.values(), Decimal(0))
        return tuple(
            ShowbackLine(value=value, amount=_money(amount), share_pct=_pct(amount, total))
            for value, amount in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
        )

    def summary(self) -> str:
        """One line for the log: what was split, and what could not be."""
        head = (
            f"{self.cluster}: {self.total} {self.currency} over {self.nodes_priced} "
            f"priced node(s), {len(self.lines)} namespace(s), "
            f"{self.unrequested_pct}% unrequested"
        )
        if self.unpriced_nodes:
            head += f" — no cost record: {', '.join(self.unpriced_nodes)}"
        if self.unmapped_nodes:
            head += f" — unmapped: {', '.join(self.unmapped_nodes)}"
        return head


def split(
    mapping: ClusterMapping,
    pods: list[Pod],
    records: list[CostRecord],
    *,
    labels: dict[str, dict[str, str]] | None = None,
    pending_pods: int = 0,
) -> NamespaceShowback:
    """Divide the cost of this cluster's nodes between the namespaces that requested it."""
    prices = Prices(records)
    ns_labels = labels or {}
    by_node = _pods_by_node(pods)

    amounts: dict[str, Decimal] = defaultdict(Decimal)
    unpriced_for: dict[str, int] = defaultdict(int)
    kubelet = unrequested = storage = Decimal(0)
    priced_nodes = 0
    unpriced: list[str] = []
    overrequested: list[str] = []
    window: tuple[date, date] | None = None
    currency = ""

    for match in mapping.matched:
        node = match.node
        pods_here = by_node.get(node.name) or by_node.get(node.short_name) or []
        cost = prices.of(match.target)
        if cost is None:
            unpriced.append(node.name or "<unnamed>")
            for namespace in {pod.namespace for pod in pods_here}:
                unpriced_for[namespace] += 1
            continue
        priced_nodes += 1
        currency = currency or cost.currency
        window = _widen(window, cost.start, cost.end)
        weights = prices.weights(match.target)
        node_cpu, node_ram = _parts(cost.amount, weights)
        storage += cost.amount - node_cpu - node_ram

        reserved = _share(node_cpu, _gap(node.vcpu, node.allocatable_vcpu), node.vcpu) + _share(
            node_ram, _gap(node.ram_gib, node.allocatable_ram_gib), node.ram_gib
        )
        kubelet += reserved
        budget = node_cpu + node_ram - reserved
        claims = {
            namespace: _share(node_cpu, cpu, node.vcpu) + _share(node_ram, ram, node.ram_gib)
            for namespace, (cpu, ram) in _requests_by_namespace(pods_here).items()
        }
        claimed = sum(claims.values(), Decimal(0))
        if claimed > budget:
            # static pods and a shrunk node can both put requests above what the node has.
            # the claims are scaled back to the money that exists and the node is named —
            # billing more than the vm costs would make the table disagree with the invoice
            overrequested.append(node.name or "<unnamed>")
            claims = {ns: value * budget / claimed for ns, value in claims.items()}
            claimed = budget
        for namespace, value in claims.items():
            amounts[namespace] += value
        unrequested += budget - claimed

    # a node nobody could place is a node whose cost we do not know either, so its
    # namespaces are counted the same way: named, with the amount flagged as partial
    for miss in mapping.unmapped:
        for namespace in {pod.namespace for pod in by_node.get(miss.node.name) or []}:
            unpriced_for[namespace] += 1

    # a namespace living only on those nodes has no amount yet, and still belongs here
    for namespace in unpriced_for:
        amounts.setdefault(namespace, Decimal(0))

    requests = _requests_by_namespace(pods)
    counts = _pod_counts(pods)
    total = _money(
        sum(amounts.values(), Decimal(0)) + kubelet + unrequested + storage
    )
    lines = tuple(
        NamespaceLine(
            namespace=namespace,
            amount=_money(amount),
            share_pct=_pct(amount, total),
            vcpu=requests.get(namespace, (Decimal(0), Decimal(0)))[0],
            ram_gib=requests.get(namespace, (Decimal(0), Decimal(0)))[1],
            pods=counts.get(namespace, 0),
            labels=dict(ns_labels.get(namespace) or {}),
            unpriced_nodes=unpriced_for.get(namespace, 0),
        )
        for namespace, amount in sorted(amounts.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    start, end = window or (date.today(), date.today())
    return NamespaceShowback(
        cluster=mapping.cluster,
        currency=currency or _USD,
        start=start,
        end=end,
        total=total,
        lines=lines,
        kubelet=_money(kubelet),
        unrequested=_money(unrequested),
        node_storage=_money(storage),
        nodes_priced=priced_nodes,
        unpriced_nodes=tuple(unpriced),
        unmapped_nodes=tuple(u.node.name or "<unnamed>" for u in mapping.unmapped),
        pending_pods=pending_pods,
        overrequested_nodes=tuple(overrequested),
    )


def _parts(amount: Decimal, weights: dict[str, Decimal]) -> tuple[Decimal, Decimal]:
    """The cpu and ram money of one node. Whatever is left is the card's storage share."""
    return amount * weights.get("cpu", Decimal(0)), amount * weights.get("ram", Decimal(0))


def _share(part: Decimal, used: Decimal, capacity: Decimal) -> Decimal:
    """Uncapped on purpose — the overshoot is what names an overrequested node."""
    if capacity <= 0 or used <= 0:  # a node that will not say how big it is splits nothing
        return Decimal(0)
    return part * used / capacity


def _gap(capacity: Decimal, allocatable: Decimal) -> Decimal:
    # allocatable 0 means the node did not report it, not that nothing is schedulable
    if allocatable <= 0 or allocatable >= capacity:
        return Decimal(0)
    return capacity - allocatable


def _pods_by_node(pods: list[Pod]) -> dict[str, list[Pod]]:
    out: dict[str, list[Pod]] = defaultdict(list)
    for pod in pods:
        out[pod.node].append(pod)
    return out


def _requests_by_namespace(pods: list[Pod]) -> dict[str, tuple[Decimal, Decimal]]:
    out: dict[str, tuple[Decimal, Decimal]] = {}
    for pod in pods:
        cpu, ram = out.get(pod.namespace, (Decimal(0), Decimal(0)))
        out[pod.namespace] = (cpu + pod.vcpu, ram + pod.ram_gib)
    return out


def _pod_counts(pods: list[Pod]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for pod in pods:
        out[pod.namespace] += 1
    return out


def _widen(window: tuple[date, date] | None, start: date, end: date) -> tuple[date, date]:
    if window is None:
        return (start, end)
    return (min(window[0], start), max(window[1], end))


def _money(amount: Decimal) -> Decimal:
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


def _pct(part: Decimal, total: Decimal) -> Decimal:
    if total <= 0:
        return Decimal(0)
    return (part / total * 100).quantize(_TENTH, rounding=ROUND_HALF_UP)
