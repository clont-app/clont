"""PersistentVolumes, classified by *whose space they occupy*.

A claim says how much; the volume behind it says where. That is the whole point of reading
this object: the on-prem pass can only see datastore space no vm accounts for, and until a
cluster says "those blocks are mine" the hypervisor half blames isos and dead vm folders
for a kubernetes volume. Same split as the other readers — `client.py` does the read,
nothing here knows a kubernetes type.

Four placements, and the arithmetic downstream turns on which one a volume got:

| kind | what it is | is it a vm's disk? |
|---|---|---|
| `datastore` | a vmdk/fcd on an array the site mounts (vsphere csi, in-tree) | while a pod holds it |
| `in-node` | a path inside a node vm's own disk (`local`, `hostPath`) | always |
| `external` | nfs, iscsi, ceph, a cloud disk | never, and not the site's space either |
| `unknown` | a csi driver we have no rule for | unknown, so nothing is subtracted |

Decisions worth keeping:

* **an unrecognised driver is `unknown`, never assumed to be on a datastore.** The gap this
  feeds is a real finding, and shrinking it on a guess would hide waste; leaving a volume
  out only leaves the gap as large as it was before kubernetes was read.
* **a file volume is never a vm's disk.** A CNS *file* volume (vsan file share, the RWX
  shape) is mounted over the network by every node that uses it, so its blocks sit on the
  datastore and belong to no vm whether a pod holds it or not. A block volume is attached
  to the node vm while a pod has it, and vcenter then counts it in that vm's committed
  storage — so only the detached ones are the site's unaccounted space. One flag,
  `attachable`, carries that difference.
* **the datastore is joined on the url, not the name.** CSI writes
  `volumeAttributes.datastoreurl` (`ds:///vmfs/volumes/<uuid>/`), which is exactly
  `Datastore.url` on the vcenter side; the in-tree plugin writes `[ds1] kubevols/x.vmdk`,
  where the name in brackets is all there is. Both are kept, and either may be empty — a
  volume clont cannot place on one datastore is still placed on *the site*.
* **`Released` is a phase worth carrying.** With `reclaimPolicy: Retain` a deleted pvc
  leaves the volume and its data behind: no claim, no pod, no vm — space nothing in either
  world accounts for. That is the one shape no pvc sweep can ever find.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from clont.providers.k8s.nodes import BYTES_PER_GIB, quantity
from clont.providers.k8s.volumes import stamp

ON_DATASTORE = "datastore"
IN_NODE = "in-node"
EXTERNAL = "external"
UNKNOWN = "unknown"

BOUND = "Bound"
RELEASED = "Released"
RETAIN = "Retain"

HOSTNAME_LABEL = "kubernetes.io/hostname"

# csi drivers whose volumes are a file on a datastore the hypervisor also mounts
DATASTORE_DRIVERS = frozenset({"csi.vsphere.vmware.com"})
# ...and the ones that cut a volume out of the node's own disk, so it is already in the
# node vm's committed space and must not be subtracted from anything
IN_NODE_DRIVERS = frozenset(
    {"topolvm.io", "local.csi.openebs.io", "openebs.io/local", "rancher.io/local-path"}
)

# in-tree sources, by the spec key they appear under
_IN_TREE_DATASTORE = ("vsphereVolume",)
_IN_TREE_IN_NODE = ("local", "hostPath")
_IN_TREE_EXTERNAL = (
    "nfs",
    "iscsi",
    "fc",
    "rbd",
    "cephfs",
    "glusterfs",
    "azureDisk",
    "azureFile",
    "awsElasticBlockStore",
    "gcePersistentDisk",
    "portworxVolume",
)
# a share is mounted over the network, so no vm ever holds it
_SHARES = ("nfs", "cephfs", "glusterfs", "azureFile")


@dataclass(frozen=True, slots=True)
class Volume:
    """One pv: how big, whose claim, and whose blocks."""

    name: str
    gib: Decimal = Decimal(0)
    phase: str = ""              # Bound / Released / Available / Failed
    claim: str = ""              # `namespace/name` it is, or was, bound to
    kind: str = UNKNOWN
    driver: str = ""             # the csi driver, or the in-tree key
    datastore: str = ""          # datastore name, when the volume path names one
    datastore_url: str = ""      # `ds:///…`, the csi join onto the vcenter's own url
    handle: str = ""             # volumeHandle / volumePath — what an operator greps for
    node: str = ""               # an in-node volume: the node whose disk holds it
    attachable: bool = True      # false for a share: never counted in a vm's disk
    reclaim: str = ""            # Retain / Delete
    created: datetime | None = None

    @property
    def on_datastore(self) -> bool:
        return self.kind == ON_DATASTORE

    @property
    def released(self) -> bool:
        """Bound to nothing any more, and still holding its blocks."""
        return self.phase == RELEASED

    def age_days(self, now: datetime | None = None) -> Decimal | None:
        """Days since the volume was created, or None when the api gave no timestamp.

        The volume's own age, not the age of the release: nothing in the object says when
        the claim went away, so a report must not imply it does.
        """
        if self.created is None:
            return None
        moment = now or datetime.now(timezone.utc)
        return Decimal(str((moment - self.created).total_seconds() / 86400))


def build_volumes(items: Iterable[dict]) -> list[Volume]:
    """`list_persistent_volume().items` as clont sees it."""
    return [_volume(item) for item in items if isinstance(item, dict)]


def _volume(item: dict) -> Volume:
    meta = _sub(item, "metadata")
    spec = _sub(item, "spec")
    status = _sub(item, "status")
    placed = _place(spec)
    claim = _sub(spec, "claimRef")
    namespace = _text(claim.get("namespace"))
    name = _text(claim.get("name"))
    return Volume(
        name=_text(meta.get("name")),
        gib=quantity(_sub(spec, "capacity").get("storage")) / BYTES_PER_GIB,
        phase=_text(status.get("phase")),
        claim=f"{namespace}/{name}" if namespace and name else "",
        reclaim=_text(spec.get("persistentVolumeReclaimPolicy")),
        created=stamp(meta.get("creationTimestamp")),
        node=_affinity_node(spec),
        **placed,
    )


def _place(spec: dict) -> dict:
    """Which of the four placements this volume's source is, and what names it."""
    csi = _sub(spec, "csi")
    if csi:
        return _csi(csi)
    for key in _IN_TREE_DATASTORE:
        source = _sub(spec, key)
        if source:
            path = _text(source.get("volumePath"))
            return {
                "kind": ON_DATASTORE,
                "driver": key,
                "handle": path,
                "datastore": _bracketed(path),
                "datastore_url": "",
                "attachable": True,
            }
    for key in _IN_TREE_IN_NODE:
        source = _sub(spec, key)
        if source:
            return {"kind": IN_NODE, "driver": key, "handle": _text(source.get("path"))}
    for key in _IN_TREE_EXTERNAL:
        if _sub(spec, key):
            return {"kind": EXTERNAL, "driver": key, "attachable": key not in _SHARES}
    return {"kind": UNKNOWN}


def _csi(csi: dict) -> dict:
    driver = _text(csi.get("driver"))
    attrs = _sub(csi, "volumeAttributes")
    if driver in DATASTORE_DRIVERS:
        return {
            "kind": ON_DATASTORE,
            "driver": driver,
            "handle": _text(csi.get("volumeHandle")),
            "datastore_url": _text(attrs.get("datastoreurl")),
            "datastore": _text(attrs.get("datastore")),
            # "vSphere CNS File Volume" is an nfs export off the datastore: the blocks are
            # there, no vm ever attaches them
            "attachable": "file" not in _text(attrs.get("type")).lower(),
        }
    kind = IN_NODE if driver in IN_NODE_DRIVERS else UNKNOWN
    return {"kind": kind, "driver": driver, "handle": _text(csi.get("volumeHandle"))}


def _affinity_node(spec: dict) -> str:
    """The node a local volume is pinned to, off `nodeAffinity` — "" when it names none."""
    required = _sub(_sub(spec, "nodeAffinity"), "required")
    terms = required.get("nodeSelectorTerms")
    for term in terms if isinstance(terms, list) else []:
        if not isinstance(term, dict):
            continue
        expressions = term.get("matchExpressions")
        for expression in expressions if isinstance(expressions, list) else []:
            if not isinstance(expression, dict):
                continue
            if _text(expression.get("key")) != HOSTNAME_LABEL:
                continue
            values = expression.get("values")
            if isinstance(values, list) and values:
                return _text(values[0])
    return ""


def _bracketed(path: str) -> str:
    """`[ds1] kubevols/x.vmdk` -> `ds1`. The datastore name is all an in-tree path gives."""
    if not path.startswith("["):
        return ""
    return path[1 : path.index("]")].strip() if "]" in path else ""


def _sub(item: dict, key: str) -> dict:
    value = item.get(key)
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()
