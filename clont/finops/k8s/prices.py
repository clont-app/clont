"""What the pricing provider already said a node costs, and the per-unit rate behind it.

Shared by the two halves of the k8s step — `namespaces.py` divides a node's cost between
namespaces, `workloads.py` prices what a shrunk request hands back — because both of them
need the same thing and neither may invent it. The whole module reads records somebody else
emitted; nothing here computes a price.

Two things it decides:

* **a record is matched on any id that identifies the resource**, because which one that is
  depends on the provider: a `moref` dimension on own iron, an instance id in a cloud, the
  resource line's own id otherwise. The alias is part of the key — two sites can hold the
  same vm name.
* **a per-unit rate is derived from the record's own window**, never assumed to be a month.
  An on-prem record is a day's run-rate stamped on one day and a CUR record is a daily
  aggregate, so a monthly figure is `amount / days * 730/24`. Reading a day as a month is
  the one arithmetic error here that would be off by thirty and still look plausible.
* **the pool's own card is read off its record's dimensions, not recomputed.** The on-prem
  collector already publishes `rate_storage_gib_month` and `overcommit_vcpu` next to the
  pool line, so the $/GiB a claim is priced at and the hypervisor's oversubscription both
  come from the same place the vm prices did. A cloud pool has no such line and answers
  None, which is the honest shape: there is nothing to read.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from clont.finops.k8s.mapping import Priced
from clont.finops.models import CostRecord
from clont.finops.onprem.rates import HOURS_PER_MONTH
from clont.providers.k8s.nodes import Node

# only used when no pool line is in the batch to read the real card off
FALLBACK_WEIGHTS = {"cpu": Decimal("0.5"), "ram": Decimal("0.5")}

_DAY_HOURS = Decimal(24)


@dataclass(frozen=True, slots=True)
class Cost:
    """One priced thing's cost over the window the records cover."""

    amount: Decimal
    currency: str
    start: date
    end: date

    @property
    def days(self) -> int:
        """Inclusive, because a record stamped on one day covers that day."""
        return max(1, (self.end - self.start).days + 1)

    @property
    def monthly(self) -> Decimal:
        return self.amount / self.days * Decimal(HOURS_PER_MONTH) / _DAY_HOURS


@dataclass(frozen=True, slots=True)
class NodeRates:
    """What one vcpu and one GiB of *this* node cost per month, off the card that priced it."""

    per_vcpu: Decimal
    per_gib: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class PoolCard:
    """What the pool line said about itself, for the questions a node cannot answer.

    `overcommit_vcpu` is the hypervisor's: the vms' reserved vcpu over the hosts' cores. A
    cluster's unrequested node capacity on top of that is the double-overcommit case.
    """

    pool: str
    currency: str
    storage_gib_month: Decimal = Decimal(0)   # the card's own $/GiB-month
    overcommit_vcpu: Decimal = Decimal(0)     # 0 means the pool did not report it
    overcommit_ram: Decimal = Decimal(0)


class Prices:
    """The cycle's cost records, indexed by everything a node could be matched on."""

    def __init__(self, records: list[CostRecord]) -> None:
        self._by_key: dict[tuple[str, str], list[CostRecord]] = defaultdict(list)
        self._weights: dict[tuple[str, str], dict[str, Decimal]] = {}
        self._cards: dict[tuple[str, str], PoolCard] = {}
        for record in records:
            alias = record.alias or ""
            dims = record.dimensions or {}
            for key in record_keys(record):
                self._by_key[(alias, key)].append(record)
            if dims.get("weights") and dims.get("cluster"):
                self._weights[(alias, dims["cluster"])] = parse_weights(dims["weights"])
            if dims.get("cluster") and dims.get("rate_storage_gib_month"):
                self._cards[(alias, dims["cluster"])] = PoolCard(
                    pool=dims["cluster"],
                    currency=record.cost.currency,
                    storage_gib_month=_number(dims.get("rate_storage_gib_month")),
                    overcommit_vcpu=_number(dims.get("overcommit_vcpu")),
                    overcommit_ram=_number(dims.get("overcommit_ram")),
                )

    def of(self, target: Priced) -> Cost | None:
        """One priced thing's cost for this window, or None when nothing carries it."""
        hits = self._by_key.get((target.alias, target.uid)) or self._by_key.get(
            (target.alias, target.name)
        )
        if not hits:
            return None
        amount = sum((hit.cost.amount for hit in hits), Decimal(0))
        return Cost(
            amount=amount,
            currency=hits[0].cost.currency,
            start=min(hit.period.start for hit in hits),
            end=max(hit.period.end for hit in hits),
        )

    def weights(self, target: Priced) -> dict[str, Decimal]:
        return self._weights.get((target.alias, target.pool)) or FALLBACK_WEIGHTS

    def card(self, target: Priced) -> PoolCard | None:
        """The pool line behind this target, or None when the provider publishes none."""
        return self.card_of(target.alias, target.pool)

    def card_of(self, alias: str, pool: str) -> PoolCard | None:
        """The same card by its two keys, for a claim that knows its pool but no node."""
        return self._cards.get((alias, pool))

    def rates(self, target: Priced, node: Node) -> NodeRates | None:
        """Monthly $/vcpu and $/GiB for one node, or None when it is not priced at all.

        A node that will not say how big it is prices at zero for that dimension rather
        than raising: the other half of the split is still a real number.
        """
        cost = self.of(target)
        if cost is None:
            return None
        weights = self.weights(target)
        monthly = cost.monthly
        cpu = monthly * weights.get("cpu", Decimal(0))
        ram = monthly * weights.get("ram", Decimal(0))
        return NodeRates(
            per_vcpu=cpu / node.vcpu if node.vcpu > 0 else Decimal(0),
            per_gib=ram / node.ram_gib if node.ram_gib > 0 else Decimal(0),
            currency=cost.currency,
        )


def record_keys(record: CostRecord) -> set[str]:
    dims = record.dimensions or {}
    keys = {dims.get("moref", ""), dims.get("instance_id", "")}
    if record.resource is not None:
        keys.add(record.resource.resource_id)
    return {key for key in keys if key}


def _number(value: object) -> Decimal:
    """A dimension back into a number; a dimension is free text, so an unreadable one is 0."""
    try:
        return Decimal(str(value or "0"))
    except Exception:  # noqa: BLE001 - see above
        return Decimal(0)


def parse_weights(text: str) -> dict[str, Decimal]:
    """`"cpu=0.5,ram=0.3,storage=0.2"` back into numbers; an unreadable pair is skipped."""
    out: dict[str, Decimal] = {}
    for pair in text.split(","):
        key, _, value = pair.partition("=")
        try:
            out[key.strip()] = Decimal(value.strip())
        except Exception:  # noqa: BLE001 - a dimension is free text, not a contract
            continue
    return out or dict(FALLBACK_WEIGHTS)
