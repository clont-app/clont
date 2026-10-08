"""`oversized-node-pool` / `double-overcommit`: the pool against what the cluster asked.

The arithmetic is the claim, so it is asserted end to end: four node vms the operator's
card prices at 730.00/month each, pods that requested a quarter of them, and the number
that comes back is the cost of the nodes that can actually go — verified by the fit test,
not estimated from a percentage.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from clont.core.models import Cloud, CloudResource, Money, Period
from clont.finops.base import FinOpsTuning
from clont.finops.k8s.mapping import Priced, match
from clont.finops.k8s.pools import DOUBLE_OVERCOMMIT, OVERSIZED, review
from clont.finops.k8s.source import ClusterRead, KubernetesSource
from clont.finops.models import CostRecord
from clont.providers.k8s.config import KubernetesCluster
from clont.providers.k8s.nodes import Node
from clont.providers.k8s.pods import Pod
from clont.providers.onprem.inventory import Pool, SiteInventory, Vm

DAY = date(2026, 10, 6)
POOL = "dc1/prod-gen11"

# 24.00/day over 730 hours is 730.00/month per node vm
PER_NODE = Decimal("730.00")


def node(name: str, *, cap=(4, 16), taints=(), cordoned: bool = False) -> Node:
    return Node(
        name=name,
        uid=f"uid-{name}",
        vcpu=Decimal(cap[0]),
        ram_gib=Decimal(cap[1]),
        allocatable_vcpu=Decimal(cap[0]),
        allocatable_ram_gib=Decimal(cap[1]),
        hard_taints=tuple(taints),
        unschedulable=cordoned,
    )


def pod(name: str, node_name: str, *, cpu="0", ram="0", owner="ReplicaSet", ns="apps") -> Pod:
    return Pod(
        namespace=ns,
        name=name,
        node=node_name,
        vcpu=Decimal(cpu),
        ram_gib=Decimal(ram),
        owner=owner,
        owner_name=f"{name}-owner",
    )


def target(name: str) -> Priced:
    return Priced(kind="vm", uid=f"vim.VirtualMachine:{name}", name=name, alias="dc1", pool="prod-gen11")


def vm_record(name: str, amount: str = "24.00") -> CostRecord:
    return CostRecord(
        cloud=str(Cloud.ONPREM),
        service="vm",
        period=Period(start=DAY, end=DAY),
        alias="dc1",
        cost=Money(amount=Decimal(amount), currency="USD"),
        resource=CloudResource(
            cloud=Cloud.ONPREM, service="vm", resource_id=f"dc1/{name}", alias="dc1"
        ),
        dimensions={"cluster": "prod-gen11", "moref": f"vim.VirtualMachine:{name}"},
    )


def pool_record(*, overcommit: str = "1.0", storage: str = "0.08") -> CostRecord:
    return CostRecord(
        cloud=str(Cloud.ONPREM),
        service="headroom",
        period=Period(start=DAY, end=DAY),
        alias="dc1",
        cost=Money(amount=Decimal("5.00"), currency="USD"),
        dimensions={
            "cluster": "prod-gen11",
            "weights": "cpu=0.5,ram=0.3,storage=0.2",
            "rate_storage_gib_month": storage,
            "overcommit_vcpu": overcommit,
            "overcommit_ram": "0.9",
        },
    )


def run(nodes: list[Node], pods: list[Pod], *, records=None, **tune):
    mapping = match("lab", nodes, [target(n.name) for n in nodes])
    recs = records if records is not None else [*(vm_record(n.name) for n in nodes), pool_record()]
    return review(mapping, pods, recs, tuning=FinOpsTuning(**tune))


def four_nodes() -> list[Node]:
    return [node(f"kube-{i}") for i in (1, 2, 3, 4)]


def one_pod_each(nodes: list[Node]) -> list[Pod]:
    # 1 vcpu / 4 GiB per node: a quarter of the pool, spread evenly
    return [pod(f"web-{n.name}", n.name, cpu="1", ram="4") for n in nodes]


def test_the_nodes_that_can_go_are_priced_off_the_card_and_named():
    nodes = four_nodes()
    report = run(nodes, one_pod_each(nodes))
    finding = report.findings[0]
    # dropping two leaves 2 nodes with (6 vcpu, 24 GiB) free and 2 vcpu / 8 GiB to place;
    # a third would need 3 vcpu in 70% of 3, which does not fit
    assert (finding.kind, finding.ref, finding.region) == (OVERSIZED, POOL, POOL)
    assert finding.monthly == PER_NODE * 2 == Decimal("1460.00")
    assert finding.nodes == ("kube-1", "kube-2")
    assert "25.0% of their capacity" in finding.summary
    assert "bank one" in finding.summary  # the same iron is `rightsize-vm` on the vm side
    assert report.pools == 1


def test_the_headroom_knob_is_what_decides_how_many_go():
    nodes = four_nodes()
    # packing to 100% lets a third node go: 3 vcpu into the last node's 3 free
    report = run(nodes, one_pod_each(nodes), onprem_rightsize_target_pct=100.0)
    assert report.findings[0].nodes == ("kube-1", "kube-2", "kube-3")
    assert report.findings[0].monthly == PER_NODE * 3


def test_a_pool_the_cluster_actually_uses_says_nothing():
    nodes = [node("kube-1"), node("kube-2")]
    pods = [pod(f"web-{n.name}", n.name, cpu="3", ram="13") for n in nodes]
    assert run(nodes, pods).findings == ()


def test_a_tainted_control_plane_node_is_neither_capacity_nor_a_candidate():
    nodes = [
        node("master-1", taints=("node-role.kubernetes.io/control-plane",)),
        node("kube-1"),
    ]
    pods = [
        pod("apiserver", "master-1", cpu="1", ram="1", owner="Node"),
        pod("web", "kube-1", cpu="3", ram="13"),
    ]
    # the worker is full; counting the master's empty half would call the pool wasteful
    assert run(nodes, pods).findings == ()


def test_a_cordoned_node_cannot_be_dropped_and_holds_no_free_capacity():
    nodes = [node("kube-1", cordoned=True), node("kube-2")]
    pods = [pod("web", "kube-2", cpu="1", ram="4")]
    # only kube-2 takes pods, and dropping it would leave nowhere to schedule
    report = run(nodes, pods)
    assert [f.kind for f in report.findings] == []


def test_a_daemonset_pod_goes_with_its_node_and_a_bare_pod_does_not():
    nodes = [node("kube-1"), node("kube-2")]
    ds = [pod(f"exporter-{n.name}", n.name, cpu="1", ram="4", owner="DaemonSet") for n in nodes]
    # nothing floating: the daemonset's share disappears with the node it ran on
    assert run(nodes, ds).findings[0].nodes == ("kube-1",)
    bare = [pod(f"shell-{n.name}", n.name, cpu="1", ram="4", owner="") for n in nodes]
    # 1 vcpu has to move into 70% of the 3 the other node has free, which it does
    assert run(nodes, bare).findings[0].nodes == ("kube-1",)
    heavy = [pod(f"shell-{n.name}", n.name, cpu="3", ram="12", owner="") for n in nodes]
    assert run(nodes, heavy).findings == ()


def test_the_most_expensive_node_goes_first():
    nodes = four_nodes()
    records = [
        vm_record("kube-1"),
        vm_record("kube-2", "48.00"),  # twice the card share of the others
        vm_record("kube-3"),
        vm_record("kube-4"),
        pool_record(),
    ]
    report = run(nodes, one_pod_each(nodes), records=records)
    assert report.findings[0].nodes == ("kube-2", "kube-1")
    assert report.findings[0].monthly == Decimal("2190.00")


def test_a_node_with_no_cost_record_is_counted_never_dropped():
    nodes = four_nodes()
    records = [*(vm_record(n.name) for n in nodes[:3]), pool_record()]
    report = run(nodes, one_pod_each(nodes), records=records)
    assert report.unpriced_nodes == ("kube-4",)
    assert "kube-4" not in report.findings[0].nodes


def test_a_saving_under_the_floor_is_noise():
    nodes = four_nodes()
    assert run(nodes, one_pod_each(nodes), onprem_min_savings_usd=2000.0).findings == ()


def test_an_oversubscribed_pool_with_nowhere_to_shrink_is_a_risk_line_at_zero():
    nodes = [node("kube-1")]
    pods = [pod("web", "kube-1", cpu="1", ram="4")]
    records = [vm_record("kube-1"), pool_record(overcommit="2.4")]
    report = run(nodes, pods, records=records)
    finding = report.findings[0]
    assert (finding.kind, finding.monthly) == (DOUBLE_OVERCOMMIT, Decimal(0))
    assert "2.4x oversubscribed" in finding.summary
    assert "not a saving" in finding.summary


def test_the_oversubscription_rides_along_when_a_node_can_still_go():
    nodes = four_nodes()
    records = [*(vm_record(n.name) for n in nodes), pool_record(overcommit="2.4")]
    report = run(nodes, one_pod_each(nodes), records=records)
    # one finding, not two: dropping a node already lowers the oversubscription
    assert [f.kind for f in report.findings] == [OVERSIZED]
    assert "promised twice and used once" in report.findings[0].summary


def test_a_pool_with_no_card_cannot_claim_a_double_overcommit():
    nodes = [node("kube-1")]
    pods = [pod("web", "kube-1", cpu="1", ram="4")]
    assert run(nodes, pods, records=[vm_record("kube-1")]).findings == ()


def test_the_binding_dimension_decides_not_the_average():
    nodes = [node("kube-1"), node("kube-2")]
    # cpu is 12% requested, ram is 94%: the pool cannot give a node back
    pods = [pod(f"db-{n.name}", n.name, cpu="0.5", ram="15") for n in nodes]
    assert run(nodes, pods).findings == ()


# --- the source end: the finding as the recommendation everything else emits ---


class _Site:
    cloud = Cloud.ONPREM
    alias = "dc1"

    def inventory(self) -> SiteInventory:
        return SiteInventory(
            pools=(
                Pool(
                    name="prod-gen11",
                    kind="cluster",
                    datacenter=None,
                    hosts=(),
                    datastores=(),
                    vms=tuple(
                        Vm(
                            uid=f"vim.VirtualMachine:kube-{i}",
                            name=f"kube-{i}",
                            host="esx-1",
                            powered_on=True,
                            template=False,
                            vcpu=4,
                            ram_gib=Decimal(16),
                            disk_gib=Decimal(100),
                            committed_gib=Decimal(60),
                        )
                        for i in (1, 2, 3, 4)
                    ),
                ),
            )
        )


def test_the_pool_finding_comes_out_as_a_recommendation():
    nodes = four_nodes()
    read = ClusterRead(nodes=nodes, pods=one_pod_each(nodes))
    src = KubernetesSource(
        "lab", KubernetesCluster(priced_by="dc1", usage="off"), _Site(), reader=lambda: read
    )
    records = [*(vm_record(n.name) for n in nodes), pool_record()]
    rec = src.recommendations(records)[0]
    assert (rec.kind, rec.service) == (OVERSIZED, "kubernetes")
    assert (rec.resource.alias, rec.resource.resource_id) == ("lab", POOL)
    assert rec.estimated_savings.amount == Decimal("1460.00")
    # the scheduler can still refuse, so it is never a quote
    assert rec.approximate is True
