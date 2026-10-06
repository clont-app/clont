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
  anything yet. It is counted in `pending` so the report can say so, because "nothing is
  schedulable" and "nothing is requested" are very different clusters.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from clont.providers.k8s.nodes import BYTES_PER_GIB, quantity

# a pod in one of these is history; it holds nothing on the node any more
TERMINATED = ("Succeeded", "Failed")

_ALWAYS = "Always"


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


def build_pods(items: Iterable[dict]) -> tuple[list[Pod], int]:
    """`list_pod_for_all_namespaces().items` as clont sees it, plus the pending count.

    The count is the pods that asked for capacity nobody gave them — they are not in the
    list because they sit on no node, and they are not silently gone either.
    """
    pods: list[Pod] = []
    pending = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(_sub(item, "status").get("phase") or "").strip() in TERMINATED:
            continue  # history, neither priced nor pending
        pod = _pod(item)
        if pod is None:
            pending += 1
            continue
        pods.append(pod)
    return pods, pending


def _pod(item: dict) -> Pod | None:
    """One live pod, or None when it sits on no node — i.e. it is waiting, not running."""
    meta = _sub(item, "metadata")
    spec = _sub(item, "spec")
    node = str(spec.get("nodeName") or "").strip()
    if not node:
        return None
    vcpu, ram = _requests(spec)
    return Pod(
        namespace=str(meta.get("namespace") or "").strip(),
        name=str(meta.get("name") or "").strip(),
        node=node,
        vcpu=vcpu,
        ram_gib=ram,
        phase=str(_sub(item, "status").get("phase") or "").strip(),
        owner=_owner(meta.get("ownerReferences")),
    )


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


def _owner(refs: object) -> str:
    if not isinstance(refs, list):
        return ""
    for ref in refs:
        if isinstance(ref, dict) and ref.get("kind"):
            return str(ref["kind"]).strip()
    return ""


def _list(value: object) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _sub(item: dict, key: str) -> dict:
    value = item.get(key)
    return value if isinstance(value, dict) else {}
