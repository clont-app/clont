"""Which priced thing is this node running on.

This module prices nothing. It answers "whose iron is this node", and the node then
inherits a cost that was already computed from the operator's own rate card — node vm ->
cluster pool -> namespace. That is the entire difference between clont's k8s number and
metering pods off a node price a tool had to guess.

**The honest half of a mapping is the half that did not match.** A node clont cannot place
comes back in `unmapped`, with the reason, and is never priced at zero: a silent zero is how
a showback table ends up adding to less than the invoice while looking complete.

The match order, strongest first, and why it is that order:

| key | from | why it ranks here |
|---|---|---|
| `provider-id` | `spec.providerID` | a cloud controller manager wrote it off the hypervisor's own record |
| `system-uuid` | smbios, the guest's `product_uuid` | no cloud provider needed, so this is the usual on-prem hit |
| `system-uuid-byteswap` | the same, mirrored | older dmidecode prints the first three fields as stored; see `nodes.swapped` |
| `name` | `metadata.name` | a guess. usually right, occasionally expensive, and switchable off |

Two decisions worth keeping:

* **a bios uuid is not unique and an instance uuid is.** vcenter's `config.uuid` is smbios
  and a clone or a restore-from-backup can carry a duplicate; `config.instanceUuid` is
  vcenter's own and cannot. So a uuid that lands on two vms is dropped from the index and
  the node is reported `ambiguous` — attributing a namespace to a coin flip between two
  clusters is worse than one missing row.
* **both vsphere uuids are indexed, because `vsphere://` means either one.** The in-tree
  cloud provider wrote the bios uuid, the out-of-tree cpi writes the instance uuid, and
  which one a customer runs is not knowable from the node.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from clont.providers.k8s.nodes import Node, normalize_uuid, swapped
from clont.providers.onprem.inventory import SiteInventory

MATCH_PROVIDER_ID = "provider-id"
MATCH_SYSTEM_UUID = "system-uuid"
MATCH_SYSTEM_UUID_SWAPPED = "system-uuid-byteswap"
MATCH_NAME = "name"

VM = "vm"
INSTANCE = "instance"


@dataclass(frozen=True, slots=True)
class Priced:
    """Something whose cost is already known: a vm on own iron, or a cloud instance."""

    kind: str          # "vm" or "instance"
    uid: str           # what the cost record is keyed on: a moref, or an instance id
    name: str
    alias: str         # provider alias — the site or the account
    pool: str          # what prices it: a cluster key on prem, a region in cloud
    uuids: tuple[str, ...] = ()

    @property
    def ref(self) -> str:
        """How a report names it: `dc1/DC0/prod-gen11/web-01`."""
        return f"{self.alias}/{self.pool}/{self.name}"


@dataclass(frozen=True, slots=True)
class NodeMatch:
    node: Node
    target: Priced
    matched_by: str


@dataclass(frozen=True, slots=True)
class Unmapped:
    node: Node
    reason: str


@dataclass(frozen=True, slots=True)
class ClusterMapping:
    """One cluster's nodes placed on priced iron, plus the ones that could not be."""

    cluster: str
    matched: tuple[NodeMatch, ...] = ()
    unmapped: tuple[Unmapped, ...] = ()

    @property
    def nodes(self) -> int:
        return len(self.matched) + len(self.unmapped)

    @property
    def pools(self) -> tuple[str, ...]:
        """The pools this cluster's nodes sit in — a cluster may span more than one."""
        return tuple(sorted({f"{m.target.alias}/{m.target.pool}" for m in self.matched}))

    @property
    def mapped_pct(self) -> Decimal:
        if not self.nodes:
            return Decimal(0)
        return (Decimal(len(self.matched)) * 100 / Decimal(self.nodes)).quantize(Decimal("0.1"))

    def target_of(self, node_name: str) -> Priced | None:
        return next((m.target for m in self.matched if m.node.name == node_name), None)

    def by_pool(self) -> dict[str, tuple[NodeMatch, ...]]:
        """Matches grouped by the pool that prices them, which is how the split runs."""
        out: dict[str, list[NodeMatch]] = {}
        for match in self.matched:
            out.setdefault(f"{match.target.alias}/{match.target.pool}", []).append(match)
        return {pool: tuple(matches) for pool, matches in sorted(out.items())}

    def summary(self) -> str:
        """One line for the startup log: what is priced, and what is not and why."""
        head = (
            f"{self.cluster}: {len(self.matched)}/{self.nodes} node(s) mapped "
            f"({self.mapped_pct}%)"
        )
        if self.matched:
            head += f" to {', '.join(self.pools)}"
        if self.unmapped:
            misses = "; ".join(f"{u.node.name or '<unnamed>'}: {u.reason}" for u in self.unmapped)
            head += f" — unmapped: {misses}"
        return head


def targets_from_site(alias: str, site: SiteInventory) -> list[Priced]:
    """Every vm of one on-prem site, as something a node can be matched to.

    Orphan vms are in here too: vcenter admits to no host for them, so they belong to no
    pool, but a node running on one is still a vm we found — and the mapping saying
    `pool=""` is a better report than calling the node unmapped.
    """
    out = [
        Priced(
            kind=VM,
            uid=vm.uid,
            name=vm.name,
            alias=alias,
            pool=pool.key,
            uuids=_uuids(vm.instance_uuid, vm.bios_uuid),
        )
        for pool in site.pools
        for vm in pool.vms
    ]
    out.extend(
        Priced(
            kind=VM,
            uid=vm.uid,
            name=vm.name,
            alias=alias,
            pool="",
            uuids=_uuids(vm.instance_uuid, vm.bios_uuid),
        )
        for vm in site.orphan_vms
    )
    return out


def targets_from_instances(alias: str, running: Iterable[Any]) -> list[Priced]:
    """Cloud instances as match targets — duck-typed on `instance_id` and `region`.

    An eks node's `providerID` carries the instance id, so no uuid is needed: the cloud
    answers with the same id the bill is keyed on.
    """
    return [
        Priced(
            kind=INSTANCE,
            uid=str(instance.instance_id),
            name=str(instance.instance_id),
            alias=alias,
            pool=str(getattr(instance, "region", "") or ""),
        )
        for instance in running
    ]


def match(
    cluster: str,
    nodes: Sequence[Node],
    targets: Sequence[Priced],
    *,
    by_name: bool = True,
) -> ClusterMapping:
    """Place every node on a priced target, or report why it could not be placed."""
    index = _Index(targets)
    matched: list[NodeMatch] = []
    unmapped: list[Unmapped] = []
    for node in nodes:
        target, how, reason = index.lookup(node, by_name=by_name)
        if target is None:
            unmapped.append(Unmapped(node=node, reason=reason))
        else:
            matched.append(NodeMatch(node=node, target=target, matched_by=how))
    return ClusterMapping(cluster=cluster, matched=tuple(matched), unmapped=tuple(unmapped))


class _Index:
    """The three lookups, each one keeping its own duplicates so they can be reported."""

    def __init__(self, targets: Sequence[Priced]) -> None:
        self._by_uuid = _group((uuid, t) for t in targets for uuid in t.uuids)
        self._by_uid = _group((t.uid, t) for t in targets)
        self._by_name = _group((t.name.strip().lower(), t) for t in targets if t.name.strip())

    def lookup(self, node: Node, *, by_name: bool) -> tuple[Priced | None, str, str]:
        backend, value = node.provider
        if backend and value:
            hit, reason = self._resolve(value)
            if hit is not None:
                return hit, MATCH_PROVIDER_ID, ""
            if reason:
                return None, "", reason
        system = normalize_uuid(node.system_uuid)
        candidates = ((system, MATCH_SYSTEM_UUID), (swapped(system), MATCH_SYSTEM_UUID_SWAPPED))
        for candidate, how in candidates:
            if not candidate:
                continue
            hit, reason = self._pick(self._by_uuid.get(candidate, ()), f"uuid {candidate}")
            if hit is not None:
                return hit, how, ""
            if reason:
                return None, "", reason
        if by_name and node.name:
            for name in (node.name.strip().lower(), node.short_name.strip().lower()):
                hit, reason = self._pick(self._by_name.get(name, ()), f"name {name!r}")
                if hit is not None:
                    return hit, MATCH_NAME, ""
                if reason:
                    return None, "", reason
        return None, "", self._miss(node, by_name=by_name)

    def _resolve(self, value: str) -> tuple[Priced | None, str]:
        """A provider id's tail: a uuid on a hypervisor, an instance id in a cloud."""
        uuid = normalize_uuid(value)
        if uuid:
            return self._pick(self._by_uuid.get(uuid, ()), f"uuid {uuid}")
        return self._pick(self._by_uid.get(value, ()), f"id {value}")

    @staticmethod
    def _pick(hits: tuple[Priced, ...], what: str) -> tuple[Priced | None, str]:
        """One hit is a match; two is a coin flip, and a coin flip is not an answer."""
        distinct = {hit.uid: hit for hit in hits}
        if len(distinct) == 1:
            return next(iter(distinct.values())), ""
        if len(distinct) > 1:
            where = ", ".join(sorted(hit.ref for hit in distinct.values()))
            return None, f"{what} is on {len(distinct)} priced resources ({where})"
        return None, ""

    def _miss(self, node: Node, *, by_name: bool) -> str:
        backend, value = node.provider
        if backend and value:
            return f"providerID {node.provider_id} matches nothing priced"
        if node.uuids:
            return f"smbios uuid {node.uuids[0]} matches no vm, and no providerID is set"
        if not by_name:
            return "no providerID and no smbios uuid, and name matching is off"
        return "no providerID, no smbios uuid, and no vm of this name"


def _uuids(*values: str | None) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        uuid = normalize_uuid(value)
        if uuid and uuid not in out:
            out.append(uuid)
    return tuple(out)


def _group(pairs: Iterable[tuple[str, Priced]]) -> dict[str, tuple[Priced, ...]]:
    out: dict[str, list[Priced]] = {}
    for key, target in pairs:
        if key:
            out.setdefault(key, []).append(target)
    return {key: tuple(values) for key, values in out.items()}
