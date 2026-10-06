"""What a pod reserved, as the scheduler counts it.

The sums are the whole point: get the init-container rule wrong and every namespace running
a migration job is billed twice, and a terminated pod that keeps its request bills a
namespace for a cronjob that finished last tuesday.
"""

from __future__ import annotations

from decimal import Decimal

from clont.providers.k8s.pods import build_pods


def pod(name: str, **over) -> dict:
    spec = {"nodeName": "kube-1", "containers": []}
    spec.update(over.pop("spec", {}))
    return {
        "metadata": {"name": name, "namespace": over.pop("namespace", "apps")},
        "spec": spec,
        "status": {"phase": over.pop("phase", "Running")},
    }


def container(cpu: str | None = None, memory: str | None = None, **over) -> dict:
    requests = {}
    if cpu is not None:
        requests["cpu"] = cpu
    if memory is not None:
        requests["memory"] = memory
    return {"name": "c", "resources": {"requests": requests}, **over}


def test_containers_add_up_and_quantities_are_parsed():
    pods, pending = build_pods(
        [pod("web", spec={"containers": [container("500m", "256Mi"), container("1", "1Gi")]})]
    )
    assert pending == []
    assert pods[0].vcpu == Decimal("1.5")
    assert pods[0].ram_gib == Decimal("1.25")


def test_an_init_container_does_not_add_to_the_running_total():
    # it runs before the others, so the pod needs the max of the two, not the sum
    pods, _ = build_pods(
        [
            pod(
                "web",
                spec={
                    "containers": [container("500m", "256Mi")],
                    "initContainers": [container("2", "1Gi")],
                },
            )
        ]
    )
    assert pods[0].vcpu == Decimal(2)
    assert pods[0].ram_gib == Decimal(1)


def test_a_sidecar_does_add_because_it_runs_for_the_pods_whole_life():
    pods, _ = build_pods(
        [
            pod(
                "web",
                spec={
                    "containers": [container("500m", "256Mi")],
                    "initContainers": [container("250m", "256Mi", restartPolicy="Always")],
                },
            )
        ]
    )
    assert pods[0].vcpu == Decimal("0.75")
    assert pods[0].ram_gib == Decimal("0.5")


def test_pod_overhead_is_added_on_top():
    pods, _ = build_pods(
        [
            pod(
                "web",
                spec={
                    "containers": [container("1", "1Gi")],
                    "overhead": {"cpu": "100m", "memory": "128Mi"},
                },
            )
        ]
    )
    assert pods[0].vcpu == Decimal("1.1")
    assert pods[0].ram_gib == Decimal("1.125")


def test_a_terminated_pod_holds_nothing_and_is_dropped():
    pods, pending = build_pods(
        [
            pod("job-a", phase="Succeeded", spec={"containers": [container("4", "8Gi")]}),
            pod("job-b", phase="Failed", spec={"containers": [container("4", "8Gi")]}),
        ]
    )
    assert pods == []
    assert pending == []  # they are history, not waiting


def test_an_unscheduled_pod_is_counted_not_priced():
    item = pod("web", spec={"containers": [container("1", "1Gi")]})
    item["spec"]["nodeName"] = ""
    pods, pending = build_pods([item])
    assert pods == []
    assert [p.name for p in pending] == ["web"]
    assert pending[0].vcpu == Decimal(1)  # it asked, nobody gave


def test_a_pod_with_no_requests_at_all_still_appears():
    # best-effort pods are real tenants of a node; they just claim no share of its cost
    pods, _ = build_pods([pod("web", spec={"containers": [container()]})])
    assert pods[0].vcpu == Decimal(0)
    assert pods[0].ram_gib == Decimal(0)
    assert pods[0].namespace == "apps"


def test_the_owner_kind_rides_along():
    item = pod("web")
    item["metadata"]["ownerReferences"] = [{"kind": "DaemonSet", "name": "node-exporter"}]
    pods, _ = build_pods([item])
    assert pods[0].owner == "DaemonSet"


def test_junk_items_are_skipped_not_fatal():
    pods, pending = build_pods([None, "nope", pod("web")])  # type: ignore[list-item]
    assert len(pods) == 1
    assert pending == []
