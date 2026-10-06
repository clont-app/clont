"""Nodes turned into something that can be matched to iron we already price.

`client.py` does the one read (`list_node`) and hands over the api's own json; this turns
it into `Node`s and holds no kubernetes types at all. Same split as
`vsphere.py` / `inventory.py` on the on-prem side, for the same reason: the match keys are
the arguable half of the k8s source, so they have to be testable without a cluster.

The three ids a node carries, and what each one is worth:

* **`spec.providerID`** — written by a cloud controller manager, so it only exists when
  one runs. The strongest key when it is there, but `vsphere://<uuid>` is *two different
  uuids* depending on who wrote it: the in-tree provider used the vm's bios uuid
  (`config.uuid`), the out-of-tree cpi uses `config.instanceUuid`. clont reads both off
  vcenter and matches either — which cpi a customer runs is not something to guess at.
* **`status.nodeInfo.systemUUID`** — smbios, so it is there with no cloud provider at
  all, which is the common on-prem case. It comes from the guest's `product_uuid` and can
  be **byte-swapped** against the uuid vcenter holds for the same vm (see `swapped`), so
  both spellings are tried.
* **`metadata.name`** — last resort, and only when it is unique. A node called `web-01`
  next to a vm called `web-01` is a guess that is usually right and occasionally
  expensively wrong, so every match records which key answered it.

Capacity is parsed here because the namespace split divides by it later, and because
`status.capacity` is the only place a node says how much iron it offers. `allocatable` is
carried next to it: the gap is the kubelet's reservation, which is real cost nobody can
schedule into.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

BYTES_PER_GIB = Decimal(1024**3)

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# binary first: "Mi" would otherwise match the decimal "M" and come out 1024x small
_BINARY = {"Ki": 10, "Mi": 20, "Gi": 30, "Ti": 40, "Pi": 50, "Ei": 60}
_DECIMAL = {"n": -9, "u": -6, "m": -3, "k": 3, "M": 6, "G": 9, "T": 12, "P": 15, "E": 18}

# instance type and placement; the beta spellings are still what older nodes carry
_TYPE_LABELS = ("node.kubernetes.io/instance-type", "beta.kubernetes.io/instance-type")
_ZONE_LABELS = ("topology.kubernetes.io/zone", "failure-domain.beta.kubernetes.io/zone")
_REGION_LABELS = ("topology.kubernetes.io/region", "failure-domain.beta.kubernetes.io/region")


@dataclass(frozen=True, slots=True)
class Node:
    """One node, with the ids that can place it on priced iron and the iron it offers."""

    name: str
    uid: str                  # metadata.uid, unique for the life of the cluster
    provider_id: str = ""     # spec.providerID, absent without a cloud controller manager
    system_uuid: str = ""     # status.nodeInfo.systemUUID, smbios
    vcpu: Decimal = Decimal(0)
    ram_gib: Decimal = Decimal(0)
    allocatable_vcpu: Decimal = Decimal(0)
    allocatable_ram_gib: Decimal = Decimal(0)
    instance_type: str | None = None
    zone: str | None = None
    region: str | None = None
    unschedulable: bool = False
    # None when the node reports no Ready condition at all, which is not the same as False
    ready: bool | None = None
    kubelet: str | None = None

    @property
    def provider(self) -> tuple[str, str]:
        return parse_provider_id(self.provider_id)

    @property
    def uuids(self) -> tuple[str, ...]:
        """Every uuid this node could be known by, strongest first, deduped.

        The provider id's uuid comes first because a cloud controller manager wrote it off
        the hypervisor's own record; smbios comes from inside the guest, and its mirror
        image comes last because a byte-swapped collision is vanishingly unlikely but the
        straight reading is still the better answer when both hit.
        """
        system = normalize_uuid(self.system_uuid)
        out: list[str] = []
        for candidate in (normalize_uuid(self.provider[1]), system, swapped(system)):
            if candidate and candidate not in out:
                out.append(candidate)
        return tuple(out)

    @property
    def short_name(self) -> str:
        """The node name without its dns suffix — kubelet may register either spelling."""
        return self.name.split(".")[0]


def build_nodes(items: Iterable[dict]) -> list[Node]:
    """`list_node().items` as clont sees it. A node missing everything still comes back.

    A node with no ids at all is not dropped: it is exactly the row the mapping has to
    report as unmapped, and dropping it here would make a showback table look complete.
    """
    return [_node(item) for item in items if isinstance(item, dict)]


def _node(item: dict) -> Node:
    meta = _sub(item, "metadata")
    spec = _sub(item, "spec")
    status = _sub(item, "status")
    info = _sub(status, "nodeInfo")
    labels = {str(k): str(v) for k, v in _sub(meta, "labels").items()}
    capacity = _sub(status, "capacity")
    allocatable = _sub(status, "allocatable")
    name = _text(meta.get("name"))
    return Node(
        name=name,
        uid=_text(meta.get("uid")),
        provider_id=_text(spec.get("providerID")),
        system_uuid=_text(info.get("systemUUID")),
        vcpu=quantity(capacity.get("cpu")),
        ram_gib=quantity(capacity.get("memory")) / BYTES_PER_GIB,
        allocatable_vcpu=quantity(allocatable.get("cpu")),
        allocatable_ram_gib=quantity(allocatable.get("memory")) / BYTES_PER_GIB,
        instance_type=_label(labels, _TYPE_LABELS),
        zone=_label(labels, _ZONE_LABELS),
        region=_label(labels, _REGION_LABELS),
        unschedulable=bool(spec.get("unschedulable")),
        ready=_ready(status.get("conditions")),
        kubelet=_text(info.get("kubeletVersion")) or None,
    )


def parse_provider_id(value: str) -> tuple[str, str]:
    """`vsphere://42aa…` -> `("vsphere", "42aa…")`, `aws:///eu-west-1a/i-0abc` -> `("aws", "i-0abc")`.

    The id is the **last** path segment, never the first: aws puts the zone in front of
    the instance id (and an empty host segment before that), and fargate adds a cluster
    segment too.
    """
    scheme, _, rest = str(value or "").strip().partition("://")
    tail = rest.strip("/").split("/")[-1] if rest else ""
    return (scheme.strip().lower(), tail) if tail else ("", "")


def normalize_uuid(value: object) -> str:
    """A uuid in one spelling, or "" when it is not one.

    Rejecting a non-uuid matters: an aws instance id also arrives through this path, and a
    key that is not a uuid must never land in the uuid index.
    """
    text = str(value or "").strip().lower().removeprefix("urn:uuid:").strip("{}")
    return text if _UUID.fullmatch(text) else ""


def swapped(uuid: str) -> str:
    """The same uuid with its first three fields byte-reversed, or "" if it is not a uuid.

    smbios stores those fields little-endian. vcenter prints them big-endian, an older
    dmidecode prints what it read, and the guest's `product_uuid` is what kubelet reports
    as `systemUUID` — so for the same vm the two can be mirror images of each other.
    """
    clean = normalize_uuid(uuid)
    if not clean:
        return ""
    parts = clean.split("-")
    return "-".join([_flip(parts[0]), _flip(parts[1]), _flip(parts[2]), parts[3], parts[4]])


def quantity(value: object) -> Decimal:
    """A k8s resource quantity as a plain number: `"3200m"` -> 3.2, `"64Gi"` -> bytes.

    cpu comes back in cores and memory in bytes, which is what the rate card divides.
    An unparseable value is 0 rather than a raised error — a node that will not say how
    big it is still has to appear in the report.
    """
    text = str(value or "").strip()
    if not text:
        return Decimal(0)
    for suffix, power in _BINARY.items():
        if text.endswith(suffix):
            return _decimal(text[: -len(suffix)]) * (Decimal(2) ** power)
    if text[-1] in _DECIMAL:
        return _decimal(text[:-1]) * (Decimal(10) ** _DECIMAL[text[-1]])
    return _decimal(text)


def _flip(field: str) -> str:
    return "".join(reversed([field[i : i + 2] for i in range(0, len(field), 2)]))


def _decimal(text: str) -> Decimal:
    try:
        return Decimal(text.strip() or "0")
    except InvalidOperation:
        return Decimal(0)


def _ready(conditions: object) -> bool | None:
    if not isinstance(conditions, list):
        return None
    for condition in conditions:
        if isinstance(condition, dict) and _text(condition.get("type")) == "Ready":
            return _text(condition.get("status")) == "True"
    return None


def _label(labels: dict[str, str], keys: tuple[str, ...]) -> str | None:
    return next((labels[key] for key in keys if labels.get(key)), None)


def _sub(item: dict, key: str) -> dict:
    value = item.get(key)
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()
