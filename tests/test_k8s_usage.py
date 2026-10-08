"""Measured usage: the metrics-server parse, the workload fold, and the ring behind the p95.

The two properties that matter here are the honest ones. A workload's number is the
*busiest* replica's, because one template sizes all of them; and until enough samples have
accumulated there is **no row at all**, because metrics-server hands over one instant
reading and a tool that sized a deployment off it would be advising on the last 30 seconds.
"""

from __future__ import annotations

from decimal import Decimal

from clont.providers.k8s.pods import Pod, WorkloadRef, build_pods
from clont.providers.k8s.usage import (
    METRICS_SERVER,
    PROMETHEUS,
    PodUsage,
    UsageHistory,
    build_usage,
    fold,
)

WEB = WorkloadRef("apps", "Deployment", "web")


def metrics(name: str, *containers: tuple[str, str], namespace: str = "apps") -> dict:
    return {
        "metadata": {"name": name, "namespace": namespace},
        "window": "30s",
        "containers": [
            {"name": f"c{i}", "usage": {"cpu": cpu, "memory": memory}}
            for i, (cpu, memory) in enumerate(containers)
        ],
    }


def pod(name: str, *, workload: str = "web", namespace: str = "apps") -> Pod:
    return Pod(
        namespace=namespace,
        name=name,
        node="kube-1",
        owner="ReplicaSet",
        owner_name=f"{workload}-7d9f8b6c5",
        template_hash="7d9f8b6c5",
    )


def usage(name: str, cpu: str, ram_gib: str, *, samples: int = 1) -> PodUsage:
    return PodUsage(
        namespace="apps",
        name=name,
        vcpu=Decimal(cpu),
        ram_gib=Decimal(ram_gib),
        samples=samples,
    )


def test_a_pods_usage_is_the_sum_over_its_containers():
    rows = build_usage([metrics("web-1", ("250m", "128Mi"), ("1250m", "384Mi"))])
    assert rows[0].vcpu == Decimal("1.5")
    assert rows[0].ram_gib == Decimal("0.5")
    assert (rows[0].namespace, rows[0].name, rows[0].samples) == ("apps", "web-1", 1)


def test_junk_in_the_list_does_not_take_the_read_down():
    rows = build_usage([None, {"metadata": {}}, metrics("web-1", ("1", "1Gi")), "nope"])
    assert [row.name for row in rows] == ["web-1"]


def test_the_workload_is_the_busiest_replica_not_the_average():
    pods = [pod("web-1"), pod("web-2")]
    rows = fold(pods, [usage("web-1", "0.2", "1"), usage("web-2", "0.9", "0.5")], source=PROMETHEUS)
    assert rows[WEB].vcpu == Decimal("0.9")   # the hot pod's cpu
    assert rows[WEB].ram_gib == Decimal(1)    # ...and the other one's ram, maxed per dimension
    assert (rows[WEB].replicas, rows[WEB].source) == (2, PROMETHEUS)


def test_a_young_replica_does_not_make_the_workload_unmeasured():
    pods = [pod("web-1"), pod("web-2")]
    rows = fold(
        pods,
        [usage("web-1", "0.5", "1", samples=4000), usage("web-2", "0.5", "1", samples=3)],
        source=PROMETHEUS,
        min_samples=24,
    )
    assert rows[WEB].samples == 4000


def test_a_workload_with_too_little_history_has_no_row():
    rows = fold([pod("web-1")], [usage("web-1", "0.5", "1", samples=3)], source=PROMETHEUS, min_samples=24)
    assert rows == {}


def test_usage_for_a_pod_the_cluster_no_longer_lists_is_dropped():
    # prometheus remembers a deleted replica; it cannot size a live template
    rows = fold([pod("web-1")], [usage("web-9", "4", "8")], source=PROMETHEUS)
    assert rows == {}


def test_the_ring_turns_instant_samples_into_a_p95():
    history = UsageHistory()
    pods = [pod("web-1")]
    for cpu in ("0.1", "0.1", "0.1", "0.9"):  # nearest-rank p95 of 4 samples is the top one
        history.observe(pods, [usage("web-1", cpu, "1")])
    rows = history.rows(pods, min_samples=4)
    assert rows[WEB].vcpu == Decimal("0.9")
    assert (rows[WEB].samples, rows[WEB].source) == (4, METRICS_SERVER)
    # one sample short and the workload is simply not advised about
    assert history.rows(pods, min_samples=5) == {}


def test_one_observe_is_one_sample_even_with_several_replicas():
    history = UsageHistory()
    pods = [pod("web-1"), pod("web-2")]
    history.observe(pods, [usage("web-1", "0.2", "1"), usage("web-2", "0.4", "2")])
    rows = history.rows(pods, min_samples=1)
    assert rows[WEB].samples == 1
    assert (rows[WEB].vcpu, rows[WEB].ram_gib) == (Decimal("0.4"), Decimal(2))


def test_history_survives_a_rollout_because_it_is_keyed_on_the_workload():
    history = UsageHistory()
    old = [pod("web-aaa")]
    for _ in range(3):
        history.observe(old, [usage("web-aaa", "0.5", "1")])
    new = [pod("web-bbb")]  # same deployment, every pod name replaced
    history.observe(new, [usage("web-bbb", "0.5", "1")])
    assert history.rows(new, min_samples=4)[WEB].samples == 4


def test_a_workload_that_is_gone_is_forgotten():
    history = UsageHistory()
    history.observe([pod("web-1")], [usage("web-1", "0.5", "1")])
    history.observe([pod("api-1", workload="api")], [usage("api-1", "0.5", "1")])
    assert history.rows([pod("web-1")], min_samples=1) == {}


def test_the_ring_is_bounded():
    history = UsageHistory(ring=3)
    pods = [pod("web-1")]
    for _ in range(10):
        history.observe(pods, [usage("web-1", "0.5", "1")])
    assert history.rows(pods, min_samples=1)[WEB].samples == 3


def test_a_pod_with_no_metrics_row_adds_no_sample():
    history = UsageHistory()
    pods = [pod("web-1")]
    history.observe(pods, [])
    assert history.rows(pods, min_samples=1) == {}


def test_the_owner_chain_names_the_thing_an_operator_edits():
    items = [
        {
            "metadata": {
                "name": "web-7d9f8b6c5-xk2lp",
                "namespace": "apps",
                "labels": {"pod-template-hash": "7d9f8b6c5"},
                "ownerReferences": [{"kind": "ReplicaSet", "name": "web-7d9f8b6c5"}],
            },
            "spec": {"nodeName": "kube-1", "containers": []},
            "status": {"phase": "Running"},
        }
    ]
    assert build_pods(items)[0][0].workload == WEB


def test_without_the_hash_label_the_workload_stays_the_replicaset():
    # the strip is what licenses the "Deployment" claim; with nothing to match, do not guess
    bare = Pod(namespace="apps", name="web-x", node="kube-1", owner="ReplicaSet", owner_name="web-7d9f")
    assert bare.workload == WorkloadRef("apps", "ReplicaSet", "web-7d9f")


def test_a_pod_with_no_owner_is_its_own_workload():
    static = Pod(namespace="kube-system", name="etcd-kube-1", node="kube-1")
    assert static.workload == WorkloadRef("kube-system", "Pod", "etcd-kube-1")


def test_a_statefulset_or_daemonset_keeps_its_own_kind():
    for kind in ("StatefulSet", "DaemonSet", "Job"):
        owned = Pod(namespace="apps", name="x-0", node="kube-1", owner=kind, owner_name="db")
        assert owned.workload == WorkloadRef("apps", kind, "db")
