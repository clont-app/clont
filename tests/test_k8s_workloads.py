"""`rightsize-workload`: a pod template against the p95 of the busiest replica that ran it.

The money chain is the whole point and it is asserted end to end: the operator's card
priced a vm, the vm is a node, the node's cost gives a $/vcpu-month, and that is what a
shrunk request hands back. Nothing here invents a price, so a workload on a node with no
cost record comes back with no advice rather than a plausible number.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from clont.agent.runner import Agent
from clont.core.models import Cloud, CloudResource, Money, Period
from clont.finops.base import FinOpsTuning
from clont.finops.k8s.mapping import Priced, match
from clont.finops.k8s.namespaces import NamespaceShowback
from clont.finops.k8s.source import ClusterRead, KubernetesSource
from clont.finops.k8s.workloads import RIGHTSIZE, UNDERREQUESTED, advise
from clont.finops.models import CostRecord, Recommendation
from clont.providers.k8s.config import KubernetesCluster
from clont.providers.k8s.nodes import Node
from clont.providers.k8s.pods import Pod, WorkloadRef
from clont.providers.k8s.usage import METRICS_SERVER, PodUsage, WorkloadUsage
from clont.providers.onprem.inventory import Pool, SiteInventory, Vm

DAY = date(2026, 10, 6)
MOREF = "vim.VirtualMachine:vm-1"
WEB = WorkloadRef("apps", "Deployment", "web")

# the chain under every number below: 24.00/day is 730.00/month (730h), the card puts 50% of
# it on cpu and 30% on ram, and the node is 4 vcpu / 16 GiB
#   per vcpu-month = 730 * 0.5 / 4  = 91.25
#   per GiB-month  = 730 * 0.3 / 16 = 13.6875
PER_VCPU = Decimal("91.25")
PER_GIB = Decimal("13.6875")


def node(name: str = "kube-1", *, cap=(4, 16)) -> Node:
    return Node(
        name=name,
        uid=f"uid-{name}",
        vcpu=Decimal(cap[0]),
        ram_gib=Decimal(cap[1]),
        allocatable_vcpu=Decimal(cap[0]),
        allocatable_ram_gib=Decimal(cap[1]),
    )


def target(uid: str = MOREF, name: str = "kube-1") -> Priced:
    return Priced(kind="vm", uid=uid, name=name, alias="dc1", pool="prod-gen11")


def mapped(nodes: list[Node] | None = None, targets: list[Priced] | None = None):
    return match("lab", nodes or [node()], targets if targets is not None else [target()])


def pod(name: str, *, cpu="0", ram="0", workload="web", node_name="kube-1", ns="apps") -> Pod:
    return Pod(
        namespace=ns,
        name=name,
        node=node_name,
        vcpu=Decimal(cpu),
        ram_gib=Decimal(ram),
        owner="ReplicaSet",
        owner_name=f"{workload}-7d9f",
        template_hash="7d9f",
    )


def used(cpu: str, ram: str, *, samples: int = 48, replicas: int = 2) -> WorkloadUsage:
    return WorkloadUsage(
        vcpu=Decimal(cpu),
        ram_gib=Decimal(ram),
        samples=samples,
        replicas=replicas,
        source=METRICS_SERVER,
    )


def vm_record(amount: str = "24.00", *, day: date = DAY, moref: str = MOREF) -> CostRecord:
    return CostRecord(
        cloud=str(Cloud.ONPREM),
        service="vm",
        period=Period(start=day, end=day),
        alias="dc1",
        cost=Money(amount=Decimal(amount), currency="USD"),
        resource=CloudResource(
            cloud=Cloud.ONPREM, service="vm", resource_id="dc1/kube-1", alias="dc1"
        ),
        dimensions={"cluster": "prod-gen11", "moref": moref},
    )


def pool_record(weights: str = "cpu=0.5,ram=0.3,storage=0.2") -> CostRecord:
    return CostRecord(
        cloud=str(Cloud.ONPREM),
        service="headroom",
        period=Period(start=DAY, end=DAY),
        alias="dc1",
        cost=Money(amount=Decimal("5.00"), currency="USD"),
        dimensions={"cluster": "prod-gen11", "weights": weights},
    )


def records() -> list[CostRecord]:
    return [vm_record(), pool_record()]


def run(pods, usage, recs=None, **tune):
    return advise(
        mapped(),
        pods,
        usage,
        recs if recs is not None else records(),
        tuning=FinOpsTuning(**tune),
        source=METRICS_SERVER,
    )


def test_cpu_is_shrunk_to_the_p95_plus_headroom_and_priced_on_the_card():
    pods = [pod("web-1", cpu="1", ram="1"), pod("web-2", cpu="1", ram="1")]
    report = run(pods, {WEB: used("0.35", "0.7")})
    finding = report.findings[0]
    assert (finding.kind, finding.ref, finding.region) == (RIGHTSIZE, "apps/deployment/web", "dc1/prod-gen11")
    # 0.35 at 70% of the new size is 0.5 vcpu, so each of the two replicas hands back 0.5
    assert finding.monthly == Decimal("0.5") * 2 * PER_VCPU == Decimal("91.25")
    assert finding.replicas == 2
    assert "500m / 1,024 Mi" in finding.summary
    assert report.measured == 1


def test_both_dimensions_move_independently():
    pods = [pod("web-1", cpu="1", ram="2"), pod("web-2", cpu="1", ram="2")]
    report = run(pods, {WEB: used("0.35", "0.7")})
    # cpu 0.5 and ram 1 GiB advised: 2 * (0.5 * 91.25) + 2 * (1 * 13.6875)
    assert report.findings[0].monthly == Decimal("118.63")


def test_a_template_already_at_the_target_gets_no_finding():
    pods = [pod("web-1", cpu="0.5", ram="1")]
    report = run(pods, {WEB: used("0.35", "0.7", replicas=1)})
    assert report.findings == ()
    assert report.measured == 1


def test_a_dimension_that_asks_for_nothing_is_left_alone():
    # no cpu request: there is nothing to hand back, and inventing one is another finding
    pods = [pod("web-1", cpu="0", ram="4"), pod("web-2", cpu="0", ram="4")]
    report = run(pods, {WEB: used("0.9", "0.7")})
    assert "0m / 0 Mi requested" not in report.findings[0].summary
    # only ram moves: 4 GiB down to 1 GiB on both replicas, 2 * 3 * 13.6875
    assert report.findings[0].monthly == Decimal("82.13")


def test_a_workload_that_requests_nothing_at_all_is_counted_not_advised():
    report = run([pod("web-1")], {WEB: used("0.5", "1", replicas=1)})
    assert (report.findings, report.norequest, report.measured) == ((), 1, 1)


def test_no_measurement_means_no_row():
    pods = [pod("web-1", cpu="4", ram="8")]
    report = run(pods, {})
    assert (report.findings, report.unmeasured, report.measured) == ((), 1, 0)
    assert "not measured yet" in report.summary()


def test_ram_above_its_request_is_a_risk_at_zero_not_a_saving():
    pods = [pod("web-1", cpu="2", ram="1")]
    report = run(pods, {WEB: used("0.1", "1.5", replicas=1)})
    finding = report.findings[0]
    assert (finding.kind, finding.monthly) == (UNDERREQUESTED, Decimal(0))
    assert "evicted" in finding.summary
    # and the cpu advice is not emitted alongside it: fix the memory request first
    assert len(report.findings) == 1


def test_cpu_above_its_request_is_normal_and_not_a_finding():
    pods = [pod("web-1", cpu="0.1", ram="4")]
    report = run(pods, {WEB: used("0.9", "0.7", replicas=1)})
    assert [f.kind for f in report.findings] == [RIGHTSIZE]
    assert report.findings[0].monthly == Decimal("41.06")  # 3 GiB of ram, one replica


def test_a_replica_on_an_unpriced_node_hands_back_nothing_and_is_counted():
    pods = [pod("web-1", cpu="1", ram="1"), pod("web-2", cpu="1", ram="1")]
    report = advise(mapped(), pods, {WEB: used("0.35", "0.7")}, [pool_record()])
    assert (report.findings, report.unpriced_replicas) == ((), 2)
    assert "unpriced nodes" in report.summary()


def test_each_replica_is_priced_on_the_node_it_sits_on():
    pods = [pod("web-1", cpu="1", ram="1"), pod("web-2", cpu="1", ram="1", node_name="kube-9")]
    report = advise(
        match("lab", [node(), node("kube-9")], [target()]),
        pods,
        {WEB: used("0.35", "0.7")},
        records(),
    )
    # kube-9 maps to nothing priced, so only the one replica's handback is money
    assert report.findings[0].monthly == Decimal("45.63")  # 0.5 vcpu + 0 ram, one replica
    assert report.unpriced_replicas == 1


def test_a_saving_under_the_floor_is_noise():
    pods = [pod("web-1", cpu="1", ram="1")]
    report = run(pods, {WEB: used("0.35", "0.7", replicas=1)}, onprem_min_savings_usd=1000.0)
    assert report.findings == ()


def test_the_target_knob_is_the_same_one_rightsize_vm_uses():
    pods = [pod("web-1", cpu="2", ram="1")]
    tight = run(pods, {WEB: used("0.35", "0.7", replicas=1)}, onprem_rightsize_target_pct=35.0)
    # 0.35 at 35% of the new size is a whole vcpu, so less is handed back than at 70%
    assert tight.findings[0].monthly == PER_VCPU == Decimal("91.25")


def test_a_day_of_records_is_read_as_a_days_run_rate_not_a_month():
    pods = [pod("web-1", cpu="1", ram="1")]
    one_day = run(pods, {WEB: used("0.35", "0.7", replicas=1)})
    # the same 24.00/day stamped on two days must price identically
    two_days = advise(
        mapped(),
        pods,
        {WEB: used("0.35", "0.7", replicas=1)},
        [vm_record(), vm_record(day=date(2026, 10, 7)), pool_record()],
    )
    assert one_day.findings[0].monthly == two_days.findings[0].monthly == Decimal("45.63")


def test_a_request_is_rounded_to_something_a_human_would_type():
    pods = [pod("web-1", cpu="1", ram="1")]
    report = run(pods, {WEB: used("0.137", "0.031", replicas=1)})
    # 0.137/0.7 = 195.7m -> 200m, and 31.7 Mi -> 48 Mi, both up to the next step
    assert "200m / 48 Mi leaves the peak" in report.findings[0].summary


def test_two_workloads_are_ranked_by_what_they_hand_back():
    pods = [pod("web-1", cpu="2", ram="1"), pod("api-1", cpu="1", ram="1", workload="api")]
    api = WorkloadRef("apps", "Deployment", "api")
    report = run(
        pods,
        {WEB: used("0.35", "0.7", replicas=1), api: used("0.35", "0.7", replicas=1)},
    )
    assert [f.ref for f in report.findings] == ["apps/deployment/web", "apps/deployment/api"]


# --- the source end: one sample per refresh, and the finding as a Recommendation ---


class _Site:
    cloud = Cloud.ONPREM
    alias = "dc1"

    def __init__(self) -> None:
        self._inventory = SiteInventory(
            pools=(
                Pool(
                    name="prod-gen11",
                    kind="cluster",
                    datacenter="DC0",
                    hosts=(),
                    datastores=(),
                    vms=(
                        Vm(
                            uid=MOREF,
                            name="kube-1",
                            host="esx-1",
                            powered_on=True,
                            template=False,
                            vcpu=4,
                            ram_gib=Decimal(16),
                            disk_gib=Decimal(100),
                            committed_gib=Decimal(60),
                        ),
                    ),
                ),
            )
        )

    def inventory(self) -> SiteInventory:
        return self._inventory


def source(pods, usage_rows, **over) -> KubernetesSource:
    config = KubernetesCluster(priced_by="dc1", **over)
    read = ClusterRead(nodes=[node()], pods=pods, usage=usage_rows)
    ticks = iter(range(0, 10_000, 1))
    return KubernetesSource(
        "lab", config, _Site(), reader=lambda: read, clock=lambda: next(ticks)
    )


def test_one_pass_is_one_sample_however_often_the_reports_ask():
    pods = [pod("web-1", cpu="1", ram="1")]
    rows = [PodUsage(namespace="apps", name="web-1", vcpu=Decimal("0.35"), ram_gib=Decimal("0.7"))]
    src = source(pods, rows, usage_min_samples=2)
    # a cached pass must not push the same reading twice and fake the history
    assert src.workloads(records()).measured == 0
    assert src.workloads(records()).measured == 0
    src.workloads(records(), refresh=True)
    assert src.workloads(records()).measured == 1


def test_the_finding_comes_out_as_the_same_recommendation_everything_else_emits():
    pods = [pod("web-1", cpu="1", ram="1")]
    rows = [PodUsage(namespace="apps", name="web-1", vcpu=Decimal("0.35"), ram_gib=Decimal("0.7"))]
    src = source(pods, rows, usage_min_samples=1)
    rec = src.recommendations(records())[0]
    assert (rec.kind, rec.service, rec.cloud) == (RIGHTSIZE, "kubernetes", str(Cloud.ONPREM))
    # the alias is the cluster: that is where an operator edits a deployment
    assert (rec.resource.alias, rec.resource.resource_id) == ("lab", "apps/deployment/web")
    assert rec.estimated_savings.amount == Decimal("45.63")
    # capacity back to the pool is not cash until a node can go, so never a quote
    assert rec.approximate is True
    assert rec.priced_region == "dc1/DC0/prod-gen11"


def test_usage_off_means_no_advice_and_still_a_priced_cluster():
    pods = [pod("web-1", cpu="4", ram="8")]
    src = source(pods, [], usage="off")
    assert src.recommendations(records()) == []
    assert src.mapping().matched  # the pricing half is untouched


def test_prometheus_without_a_url_is_a_config_error():
    with pytest.raises(ValidationError, match="prometheus_url"):
        KubernetesCluster(priced_by="dc1", usage="prometheus")


def test_an_unknown_usage_source_is_a_config_error():
    with pytest.raises(ValidationError, match="usage must be one of"):
        KubernetesCluster(priced_by="dc1", usage="datadog")


# --- and the cycle: the advice reaches the batch, and one failure costs only itself ---


class _Stub:
    name = "lab"

    def __init__(self, recs=None, *, raises: bool = False) -> None:
        self._recs = recs or []
        self._raises = raises

    def namespaces(self, records):
        return NamespaceShowback(
            cluster="lab",
            currency="USD",
            start=DAY,
            end=DAY,
            total=Decimal("10.00"),
            nodes_priced=1,
        )

    def recommendations(self, records):
        if self._raises:
            raise RuntimeError("metrics api said no")
        return self._recs


def _rec() -> Recommendation:
    return Recommendation(
        cloud=str(Cloud.ONPREM),
        service="kubernetes",
        kind=RIGHTSIZE,
        resource=CloudResource(
            cloud=Cloud.ONPREM,
            service="kubernetes",
            resource_id="apps/deployment/web",
            region="dc1/DC0/prod-gen11",
            alias="lab",
        ),
        summary="shrink it",
        estimated_savings=Money(amount=Decimal("45.63"), currency="USD"),
    )


def test_the_advice_lands_on_the_batch_and_fires_one_event():
    batch = Agent([], [], k8s_sources=[_Stub([_rec()])])._collect_batch()
    assert [r.kind for r in batch.recommendations] == [RIGHTSIZE]
    titles = [e.title for e in batch.events if "recommendation" in e.title]
    assert titles == ["[lab] Cost recommendation: kubernetes"]


def test_a_failed_sizing_pass_does_not_cost_the_namespace_table():
    batch = Agent([], [], k8s_sources=[_Stub(raises=True)])._collect_batch()
    assert batch.recommendations == []
    assert any("findings failed" in err for err in batch.errors)
    # the split still ran: its event is there
    assert any("Namespace showback" in e.title for e in batch.events)
