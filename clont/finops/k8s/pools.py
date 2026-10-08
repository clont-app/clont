"""The node pool against what the cluster actually asked of it.

The fourth piece of the plan's k8s step, and the first one that is **not** visible from
either side alone. The hypervisor sees node vms that reserve their vcpu; the cluster sees
pods that requested a fraction of it. Put the two together and the same cores are promised
twice — once by the hypervisor's oversubscription, once by the vm to a scheduler that never
handed them out:

    hosts' cores  --oversubscribed-->  node vms' vcpu  --unrequested-->  pods

Two findings come out of it, and a pool emits **at most one**:

| kind | when | what it is worth |
|---|---|---|
| `oversized-node-pool` | the pods on it still fit after dropping whole node vms | those vms' cost, off the operator's card |
| `double-overcommit` | nothing can be dropped, but the pool is oversubscribed *and* the nodes are half empty | $0 — a risk line, like `thin-overcommit` |

**Dropping a node supersedes shrinking it**, which is why the risk line only fires when no
node can go: removal already lowers the oversubscription and the unrequested share, so
reporting both would be two prices for one piece of iron.

Decisions worth keeping:

* **the fit is tested on `allocatable`, the money is split on `capacity`.** The scheduler
  can only use allocatable; the invoice covers the whole vm. Using one number for both
  would either bill the kubelet's reservation to a namespace or promise capacity no pod can
  have.
* **a tainted or cordoned node is neither free capacity nor a candidate.** A control plane
  node is not `unschedulable`, it is tainted — count its iron as room for pods and the
  arithmetic cheerfully advises deleting a master.
* **only DaemonSet (and static) pods vanish with their node.** Their requests come back on
  whatever nodes remain, so they are per-node load. Everything else — including a bare pod
  — has to fit on the remaining nodes, which is the conservative reading.
* **`onprem_rightsize_target_pct` is the headroom here too.** The pods that have to move
  must land in that share of the free allocatable left, so the advice is not "pack it to
  100%".
* **the same iron is also `rightsize-vm` on the hypervisor side.** Both are real, they are
  the same money seen twice, and the summary says to bank one — a report that silently
  summed them would double-count a node vm.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from clont.core.logging import get_logger
from clont.finops.base import FinOpsTuning
from clont.finops.k8s.mapping import ClusterMapping, NodeMatch
from clont.finops.k8s.prices import PoolCard, Prices
from clont.finops.models import CostRecord
from clont.providers.k8s.pods import Pod

log = get_logger("clont.finops.k8s.pools")

OVERSIZED = "oversized-node-pool"
DOUBLE_OVERCOMMIT = "double-overcommit"

# a pod of these is the kubelet's, one per node: it goes away with its node and comes back
# on none of the others
PER_NODE_OWNERS = ("DaemonSet", "Node")

_CENT = Decimal("0.01")
_TENTH = Decimal("0.1")
_HUNDRED = Decimal(100)
_USD = "USD"


@dataclass(frozen=True, slots=True)
class PoolFinding:
    """One pool's advice, in the same shape a workload finding has."""

    kind: str
    ref: str          # the pool, as `alias/pool` — what an operator resizes
    region: str
    summary: str
    monthly: Decimal  # the node vms that could go, or 0 for the risk line
    currency: str = _USD
    nodes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PoolReport:
    """One cluster's pools: what each is worth, and what could not be read."""

    cluster: str
    findings: tuple[PoolFinding, ...] = ()
    pools: int = 0
    unpriced_nodes: tuple[str, ...] = ()

    def summary(self) -> str:
        head = f"{self.cluster}: {len(self.findings)} pool finding(s) over {self.pools} pool(s)"
        if self.unpriced_nodes:
            head += f", no cost record for {len(self.unpriced_nodes)} node(s)"
        return head


@dataclass(frozen=True, slots=True)
class _Row:
    """One node of a pool: its iron, its cost, and the two kinds of load on it."""

    name: str
    takes_pods: bool
    allocatable: tuple[Decimal, Decimal]
    capacity: tuple[Decimal, Decimal]
    monthly: Decimal | None            # None when nothing priced it
    currency: str = _USD
    floating: tuple[Decimal, Decimal] = (Decimal(0), Decimal(0))
    per_node: tuple[Decimal, Decimal] = (Decimal(0), Decimal(0))

    @property
    def requested(self) -> tuple[Decimal, Decimal]:
        return _add(self.floating, self.per_node)

    @property
    def free(self) -> tuple[Decimal, Decimal]:
        """Allocatable the scheduler has not handed out, never negative."""
        return (
            max(self.allocatable[0] - self.requested[0], Decimal(0)),
            max(self.allocatable[1] - self.requested[1], Decimal(0)),
        )


def review(
    mapping: ClusterMapping,
    pods: list[Pod],
    records: list[CostRecord],
    *,
    tuning: FinOpsTuning | None = None,
) -> PoolReport:
    """Every pool under this cluster, asked whether the cluster still needs all of it."""
    tune = tuning or FinOpsTuning()
    prices = Prices(records)
    by_node = _pods_by_node(pods)

    findings: list[PoolFinding] = []
    unpriced: list[str] = []
    pools = mapping.by_pool()
    for pool, matches in pools.items():
        rows = [_row(match, prices, by_node) for match in matches]
        unpriced.extend(row.name for row in rows if row.monthly is None)
        finding = _review_pool(pool, rows, prices.card(matches[0].target), tune)
        if finding is not None:
            findings.append(finding)
    report = PoolReport(
        cluster=mapping.cluster,
        findings=tuple(sorted(findings, key=lambda f: (-f.monthly, f.ref))),
        pools=len(pools),
        unpriced_nodes=tuple(unpriced),
    )
    log.debug("%s", report.summary())
    return report


def _review_pool(
    pool: str, rows: list[_Row], card: PoolCard | None, tune: FinOpsTuning
) -> PoolFinding | None:
    """One pool: drop nodes if the pods fit without them, else say why it is still wasteful."""
    filled = _filled_pct(rows)
    unrequested = _HUNDRED - filled
    if unrequested < Decimal(str(tune.k8s_pool_unrequested_pct)):
        return None  # the cluster is using what it reserved, whatever the hypervisor thinks
    target = Decimal(str(tune.onprem_rightsize_target_pct)) / _HUNDRED
    drop = _droppable(rows, target)
    currency = next((row.currency for row in rows if row.monthly is not None), _USD)
    if drop:
        monthly = sum((row.monthly or Decimal(0) for row in drop), Decimal(0))
        if monthly < Decimal(str(tune.onprem_min_savings_usd)):
            return None
        return PoolFinding(
            kind=OVERSIZED,
            ref=pool,
            region=pool,
            summary=_oversized_summary(pool, rows, drop, unrequested, card, tune),
            monthly=_money(monthly),
            currency=currency,
            nodes=tuple(row.name for row in drop),
        )
    if card is not None and card.overcommit_vcpu >= Decimal(str(tune.k8s_overcommit_ratio)):
        return PoolFinding(
            kind=DOUBLE_OVERCOMMIT,
            ref=pool,
            region=pool,
            summary=_double_summary(pool, rows, unrequested, card),
            monthly=Decimal(0),
            currency=currency,
            nodes=tuple(row.name for row in rows),
        )
    return None


def _droppable(rows: list[_Row], target: Decimal) -> list[_Row]:
    """The priced nodes this pool could lose, most expensive first, while the pods still fit.

    Greedy and verified at every step: the expensive node is the one worth dropping, and
    the fit test is what stops the loop — so whatever comes back has been checked, not
    estimated.
    """
    candidates = sorted(
        (row for row in rows if row.takes_pods and row.monthly is not None),
        key=lambda row: (-(row.monthly or Decimal(0)), row.name),
    )
    drop: list[_Row] = []
    for candidate in candidates:
        attempt = drop + [candidate]
        if _fits(rows, attempt, target):
            drop = attempt
    return drop


def _fits(rows: list[_Row], drop: list[_Row], target: Decimal) -> bool:
    """Whether the pods off `drop` fit in `target` of what the remaining nodes have free."""
    names = {row.name for row in drop}
    remaining = [row for row in rows if row.name not in names]
    schedulable = [row for row in remaining if row.takes_pods]
    if not schedulable:
        return False  # a pool with nowhere left to schedule is not a smaller pool
    need = _sum(row.floating for row in drop)
    free = _sum(row.free for row in schedulable)
    return need[0] <= target * free[0] and need[1] <= target * free[1]


def _row(match: NodeMatch, prices: Prices, by_node: dict[str, list[Pod]]) -> _Row:
    node = match.node
    here = by_node.get(node.name) or by_node.get(node.short_name) or []
    cost = prices.of(match.target)
    return _Row(
        name=node.name or "<unnamed>",
        takes_pods=node.takes_pods,
        allocatable=(node.allocatable_vcpu or node.vcpu, node.allocatable_ram_gib or node.ram_gib),
        capacity=(node.vcpu, node.ram_gib),
        monthly=cost.monthly if cost is not None else None,
        currency=cost.currency if cost is not None else _USD,
        floating=_sum(
            (pod.vcpu, pod.ram_gib) for pod in here if pod.owner not in PER_NODE_OWNERS
        ),
        per_node=_sum(
            (pod.vcpu, pod.ram_gib) for pod in here if pod.owner in PER_NODE_OWNERS
        ),
    )


def _filled_pct(rows: list[_Row]) -> Decimal:
    """How full the pool is on its **binding** dimension — the fuller of cpu and ram.

    Taking the max rather than an average is the point: a pool packed on memory and empty
    on cpu cannot give a node back, and an average would say it can.

    Counted over the nodes that take pods only. A control plane node runs its static pods
    at a fraction of its vm and nothing else may land there — including it would report a
    cluster of packed workers as half empty.
    """
    open_rows = [row for row in rows if row.takes_pods]
    if not open_rows:
        return _HUNDRED
    capacity = _sum(row.capacity for row in open_rows)
    requested = _sum(row.requested for row in open_rows)
    shares = [
        requested[i] / capacity[i] * _HUNDRED for i in (0, 1) if capacity[i] > 0
    ]
    return max(shares, default=_HUNDRED).quantize(_TENTH, rounding=ROUND_HALF_UP)


def _oversized_summary(
    pool: str,
    rows: list[_Row],
    drop: list[_Row],
    unrequested: Decimal,
    card: PoolCard | None,
    tune: FinOpsTuning,
) -> str:
    names = ", ".join(row.name for row in drop)
    head = (
        f"{pool}: {len(rows)} node vm(s) carry pod requests for {_HUNDRED - unrequested}% "
        f"of their capacity, so {len(drop)} of them ({names}) can go and the pods still "
        f"fit in {tune.onprem_rightsize_target_pct:.0f}% of what is left"
    )
    if card is not None and card.overcommit_vcpu >= Decimal(str(tune.k8s_overcommit_ratio)):
        head += (
            f" — and the pool is already {card.overcommit_vcpu}x oversubscribed on vcpu, so "
            f"those cores are promised twice and used once"
        )
    return (
        f"{head}. the scheduler can still refuse: pod anti-affinity, local volumes and "
        f"topology spread are not in this arithmetic. these node vms also carry "
        f"`rightsize-vm` advice from the hypervisor side — it is the same iron, bank one"
    )


def _double_summary(
    pool: str, rows: list[_Row], unrequested: Decimal, card: PoolCard
) -> str:
    return (
        f"{pool}: the pool is {card.overcommit_vcpu}x oversubscribed on vcpu and its "
        f"{len(rows)} node vm(s) have {unrequested}% of their capacity requested by no pod "
        f"— the same cores are promised twice over and handed out once. no whole node can "
        f"go (the pods would not fit), so the move is to shrink the node vms themselves; "
        f"this is a risk and an explanation, not a saving"
    )


def _pods_by_node(pods: list[Pod]) -> dict[str, list[Pod]]:
    out: dict[str, list[Pod]] = {}
    for pod in pods:
        out.setdefault(pod.node, []).append(pod)
    return out


def _sum(pairs) -> tuple[Decimal, Decimal]:
    cpu = ram = Decimal(0)
    for one, two in pairs:
        cpu += one
        ram += two
    return cpu, ram


def _add(left: tuple[Decimal, Decimal], right: tuple[Decimal, Decimal]):
    return left[0] + right[0], left[1] + right[1]


def _money(amount: Decimal) -> Decimal:
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)
