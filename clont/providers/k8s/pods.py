"""Pods turned into the only thing the split needs: who requested how much, and where.

Same shape as `nodes.py` — `client.py` does the read, this holds no kubernetes types — and
for the same reason: the request arithmetic is the arguable half, so it has to be testable
without a cluster.

**Requests, not usage.** A namespace's share of a node is what it reserved on it, because
that is the shape the money has: the operator paid for the vm whether the pod used it or
not, and the scheduler handed out the capacity on requests alone. Measured usage is the
*next* layer (`rightsize-workload`), and it splits an unchanged number rather than moving
it, the way `onprem/costs.py` already does for vms.

Three things decided here:

* **a pod's request is the scheduler's number, not the sum of its containers.** init
  containers run before the rest, so they do not add up with them — the pod asks for
  `max(regular + sidecars, biggest init)` plus `spec.overhead`. Sidecars (init containers
  with `restartPolicy: Always`) *do* add, they run for the pod's whole life.
* **a terminated pod is dropped.** `Succeeded`/`Failed` hold no capacity on the node — a
  finished cronjob from last tuesday would otherwise keep billing its namespace forever.
* **an unscheduled pod is dropped too** (`spec.nodeName` empty): it reserved nothing on
  anything yet. It comes back in `pending` so the report can say so, because "nothing is
  schedulable" and "nothing is requested" are very different clusters. It is kept as a
  *row*, not a count: a pending pod is evidence its namespace is alive, and it still names
  the claims it is waiting for — both things the volume findings need.

And the volumes a pod holds, because a claim nothing mounts is the only way an abandoned
one can be told from a live one. A generic ephemeral volume is a claim too, named
`<pod>-<volume>` by the controller — reading it off the pod is what keeps it out of the
unmounted list.

And one thing the sizing half needs: **which workload a pod belongs to**, because a request
lives in a pod template and advice has to name the thing an operator edits. The owner chain
is read off the pod itself, never with a second api call:

* a pod owned by a **ReplicaSet** is a Deployment's pod, and the rs name is the deployment
  name plus `-<pod-template-hash>` — which the pod carries as a *label*. So the suffix is
  removed by matching that label, never by guessing at a hash-shaped tail. An argo Rollout
  lands here too and gets its own name right, which is the half an operator reads.
* **the strip is what licenses the "Deployment" claim**: with no hash label to match, the
  workload stays the ReplicaSet it says it is rather than a name we made up.
* a pod with no owner — a static pod, a bare pod — is its own workload, kind `Pod`. A Job
  stays a Job: rolling it up to its CronJob needs a second read, and the job name already
  carries the cronjob's.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from clont.providers.k8s.nodes import BYTES_PER_GIB, quantity

# a pod in one of these is history; it holds nothing on the node any more
TERMINATED = ("Succeeded", "Failed")

# the deployment controller stamps it on every pod of a replicaset
TEMPLATE_HASH = "pod-template-hash"
REPLICA_SET = "ReplicaSet"
DEPLOYMENT = "Deployment"
BARE_POD = "Pod"

_ALWAYS = "Always"


@dataclass(frozen=True, slots=True)
class WorkloadRef:
    """The thing an operator edits: a namespace, a kind and a name."""

    namespace: str
    kind: str
    name: str

    @property
    def ref(self) -> str:
        return f"{self.namespace}/{self.kind.lower()}/{self.name}"


@dataclass(frozen=True, slots=True)
class Pod:
    """One scheduled, live pod and the iron it reserved."""

    namespace: str
    name: str
    node: str
    vcpu: Decimal = Decimal(0)
    ram_gib: Decimal = Decimal(0)
    phase: str = ""
    owner: str = ""  # ownerReferences[0].kind — a daemonset's share reads differently
    owner_name: str = ""
    template_hash: str = ""  # the `pod-template-hash` label, how a rs name is shortened
    claims: tuple[str, ...] = ()  # pvc names it mounts, in this namespace

    @property
    def workload(self) -> WorkloadRef:
        """Which pod template this pod came out of — see the module docstring."""
        kind, name = self.owner, self.owner_name
        if not name:
            return WorkloadRef(self.namespace, BARE_POD, self.name)
        if kind == REPLICA_SET:
            short = _strip_hash(name, self.template_hash)
            if short != name:
                return WorkloadRef(self.namespace, DEPLOYMENT, short)
        return WorkloadRef(self.namespace, kind or BARE_POD, name)


def build_pods(items: Iterable[dict]) -> tuple[list[Pod], list[Pod]]:
    """`list_pod_for_all_namespaces().items` as clont sees it: the scheduled, and the waiting.

    The second list is the pods that asked for capacity nobody gave them — on no node, so
    priced nowhere, and not silently gone either.
    """
    pods: list[Pod] = []
    pending: list[Pod] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(_sub(item, "status").get("phase") or "").strip() in TERMINATED:
            continue  # history, neither priced nor pending
        pod = _pod(item)
        (pods if pod.node else pending).append(pod)
    return pods, pending


def _pod(item: dict) -> Pod:
    """One live pod. An empty `node` means it sits on nothing: waiting, not running."""
    meta = _sub(item, "metadata")
    spec = _sub(item, "spec")
    node = str(spec.get("nodeName") or "").strip()
    vcpu, ram = _requests(spec)
    kind, owner = _owner(meta.get("ownerReferences"))
    return Pod(
        namespace=str(meta.get("namespace") or "").strip(),
        name=str(meta.get("name") or "").strip(),
        node=node,
        vcpu=vcpu,
        ram_gib=ram,
        phase=str(_sub(item, "status").get("phase") or "").strip(),
        owner=kind,
        owner_name=owner,
        template_hash=str(_sub(meta, "labels").get(TEMPLATE_HASH) or "").strip(),
        claims=_claims(spec.get("volumes"), str(meta.get("name") or "").strip()),
    )


def _claims(volumes: object, pod_name: str) -> tuple[str, ...]:
    """The pvcs this pod mounts, deduped. Ephemeral volumes are claims under another name."""
    out: list[str] = []
    for volume in _list(volumes):
        name = str(_sub(volume, "persistentVolumeClaim").get("claimName") or "").strip()
        if not name and "ephemeral" in volume:
            # the controller names it <pod>-<volume> and owns it, so it is mounted by
            # definition — it just does not say so in the pvc reference
            suffix = str(volume.get("name") or "").strip()
            name = f"{pod_name}-{suffix}" if pod_name and suffix else ""
        if name and name not in out:
            out.append(name)
    return tuple(out)


def _requests(spec: dict) -> tuple[Decimal, Decimal]:
    """The scheduler's effective request: run-phase total vs the biggest init step.

    The init peak is taken as a straight max rather than per-step with the sidecars
    running alongside it — it only applies while the pod starts, and a report that
    charged the startup peak for a month would be wrong in the other direction.
    """
    regular = [_container(c) for c in _list(spec.get("containers"))]
    inits = _list(spec.get("initContainers"))
    sidecars = [_container(c) for c in inits if str(c.get("restartPolicy") or "") == _ALWAYS]
    steps = [_container(c) for c in inits if str(c.get("restartPolicy") or "") != _ALWAYS]
    running = _total(regular + sidecars)
    peak = max(steps, default=(Decimal(0), Decimal(0)))
    overhead = _amounts(_sub(spec, "overhead"))
    # cpu and ram are maxed independently, which is what the kubelet admits on
    return (
        max(running[0], peak[0]) + overhead[0],
        max(running[1], peak[1]) + overhead[1],
    )


def _container(container: dict) -> tuple[Decimal, Decimal]:
    return _amounts(_sub(_sub(container, "resources"), "requests"))


def _amounts(requests: dict) -> tuple[Decimal, Decimal]:
    return quantity(requests.get("cpu")), quantity(requests.get("memory")) / BYTES_PER_GIB


def _total(parts: list[tuple[Decimal, Decimal]]) -> tuple[Decimal, Decimal]:
    return (
        sum((p[0] for p in parts), Decimal(0)),
        sum((p[1] for p in parts), Decimal(0)),
    )


def _owner(refs: object) -> tuple[str, str]:
    """The first owner's kind and name — `controller: true` is not required.

    A pod has at most one controller owner in practice, and a reader that insisted on the
    flag would call a hand-written owner reference an unowned pod.
    """
    if not isinstance(refs, list):
        return "", ""
    for ref in refs:
        if isinstance(ref, dict) and ref.get("kind"):
            return str(ref["kind"]).strip(), str(ref.get("name") or "").strip()
    return "", ""


def _strip_hash(name: str, template_hash: str) -> str:
    suffix = f"-{template_hash}"
    return name[: -len(suffix)] if template_hash and name.endswith(suffix) else name


def _list(value: object) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _sub(item: dict, key: str) -> dict:
    value = item.get(key)
    return value if isinstance(value, dict) else {}
