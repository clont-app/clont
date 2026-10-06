"""The namespace split: the node's own cost divided by what each namespace asked for.

Two properties are worth more than the rest and both are asserted on every shape here:
the buckets add back up to the nodes' cost (nothing invented, nothing lost), and a node
with no cost record is *named* rather than priced at zero.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from clont.core.models import Cloud, CloudResource, Money, Period
from clont.events.detectors import NamespaceShowbackDetector
from clont.events.models import EventSeverity
from clont.finops.k8s.mapping import Priced, match
from clont.finops.k8s.namespaces import split
from clont.finops.models import CostRecord
from clont.finops.showback import UNATTRIBUTED
from clont.providers.k8s.nodes import Node
from clont.providers.k8s.pods import Pod

DAY = date(2026, 10, 6)
MOREF = "vim.VirtualMachine:vm-1"


def node(name: str = "kube-1", *, cap=(4, 16), alloc=(4, 16), **over) -> Node:
    return Node(
        name=name,
        uid=f"uid-{name}",
        system_uuid=over.pop("system_uuid", ""),
        vcpu=Decimal(cap[0]),
        ram_gib=Decimal(cap[1]),
        allocatable_vcpu=Decimal(alloc[0]),
        allocatable_ram_gib=Decimal(alloc[1]),
        **over,
    )


def target(uid: str = MOREF, name: str = "kube-1") -> Priced:
    return Priced(kind="vm", uid=uid, name=name, alias="dc1", pool="prod-gen11")


def mapped(nodes: list[Node], targets: list[Priced] | None = None):
    return match("lab", nodes, targets if targets is not None else [target()])


def pod(namespace: str, node_name: str = "kube-1", *, cpu="0", ram="0") -> Pod:
    return Pod(
        namespace=namespace,
        name=f"{namespace}-pod",
        node=node_name,
        vcpu=Decimal(cpu),
        ram_gib=Decimal(ram),
    )


def vm_record(amount: str = "10.00", *, moref: str = MOREF, alias: str = "dc1") -> CostRecord:
    return CostRecord(
        cloud=str(Cloud.ONPREM),
        service="vm",
        period=Period(start=DAY, end=DAY),
        alias=alias,
        cost=Money(amount=Decimal(amount), currency="USD"),
        resource=CloudResource(
            cloud=Cloud.ONPREM, service="vm", resource_id="dc1/kube-1", alias=alias
        ),
        dimensions={"cluster": "prod-gen11", "moref": moref},
    )


def pool_record(weights: str = "cpu=0.5,ram=0.3,storage=0.2", *, alias="dc1") -> CostRecord:
    """The headroom line the on-prem collector emits — this is where the card's weights are."""
    return CostRecord(
        cloud=str(Cloud.ONPREM),
        service="headroom",
        period=Period(start=DAY, end=DAY),
        alias=alias,
        cost=Money(amount=Decimal("5.00"), currency="USD"),
        dimensions={"cluster": "prod-gen11", "weights": weights},
    )


def added_up(report) -> Decimal:
    """Every line of the table, namespaces and buckets alike."""
    return sum((line.amount for line in report.lines), Decimal(0)) + sum(
        (amount for _, amount in report.buckets()), Decimal(0)
    )


def test_requests_split_the_node_and_the_buckets_add_back_up():
    pods = [pod("apps", cpu="2", ram="8"), pod("kube-system", cpu="1", ram="4")]
    report = split(mapped([node()]), pods, [vm_record(), pool_record()])
    amounts = {line.namespace: line.amount for line in report.lines}
    # cpu money is 50% of the vm, ram 30%, so apps gets half of each
    assert amounts == {"apps": Decimal("4.00"), "kube-system": Decimal("2.00")}
    assert report.unrequested == Decimal("2.00")
    assert report.node_storage == Decimal("2.00")  # no pod requests a vm's disk
    assert report.total == Decimal("10.00")
    assert added_up(report) == report.total


def test_the_kubelet_reservation_is_its_own_line_not_a_markup():
    report = split(
        mapped([node(cap=(4, 16), alloc=(3, 12))]),
        [pod("apps", cpu="1", ram="4")],
        [vm_record(), pool_record()],
    )
    assert report.kubelet == Decimal("2.00")
    assert report.lines[0].amount == Decimal("2.00")
    assert added_up(report) == report.total == Decimal("10.00")


def test_the_cards_own_weights_are_used_not_a_guess():
    pods = [pod("apps", cpu="4", ram="16")]  # the whole node
    heavy = split(mapped([node()]), pods, [vm_record(), pool_record("cpu=0.8,ram=0.2")])
    assert heavy.lines[0].amount == Decimal("10.00")
    assert heavy.node_storage == Decimal("0.00")
    # and with no pool line in the batch the fallback is an even cpu/ram split
    fallback = split(mapped([node()]), pods, [vm_record()])
    assert fallback.lines[0].amount == Decimal("10.00")


def test_a_node_with_no_cost_record_is_named_never_priced_at_zero():
    report = split(mapped([node()]), [pod("apps", cpu="2", ram="8")], [pool_record()])
    assert report.unpriced_nodes == ("kube-1",)
    assert report.nodes_priced == 0
    assert report.priced is False
    # the namespace is still in the table, with its requests and a zero it can explain
    line = report.lines[0]
    assert (line.namespace, line.amount, line.unpriced_nodes) == ("apps", Decimal("0.00"), 1)
    assert (line.vcpu, line.ram_gib) == (Decimal(2), Decimal(8))


def test_an_unmapped_node_counts_the_same_way_as_an_unpriced_one():
    result = mapped([node("kube-1", system_uuid=""), node("kube-9")], [target()])
    # kube-9 matches nothing priced, kube-1 matches by name
    report = split(
        result,
        [pod("apps", cpu="2", ram="8"), pod("data", "kube-9", cpu="1", ram="1")],
        [vm_record(), pool_record()],
    )
    assert report.unmapped_nodes == ("kube-9",)
    data = next(line for line in report.lines if line.namespace == "data")
    assert (data.amount, data.unpriced_nodes) == (Decimal("0.00"), 1)
    assert added_up(report) == report.total


def test_requests_above_allocatable_are_named_not_billed_negative():
    report = split(
        mapped([node(cap=(4, 16))]),
        [pod("apps", cpu="8", ram="32")],  # static pods and a shrunk node both do this
        [vm_record(), pool_record()],
    )
    assert report.overrequested_nodes == ("kube-1",)
    assert report.unrequested == Decimal("0.00")
    assert report.lines[0].amount == Decimal("8.00")  # capped at the whole node's cpu+ram


def test_the_same_money_groups_by_a_namespace_label():
    pods = [pod("apps", cpu="2", ram="8"), pod("kube-system", cpu="1", ram="4")]
    report = split(
        mapped([node()]),
        pods,
        [vm_record(), pool_record()],
        labels={"apps": {"team": "payments"}},
    )
    by_team = {line.value: line.amount for line in report.by_label("team")}
    assert by_team == {"payments": Decimal("4.00"), UNATTRIBUTED: Decimal("2.00")}


def test_pending_pods_are_reported_rather_than_priced():
    report = split(
        mapped([node()]),
        [pod("apps", cpu="2", ram="8")],
        [vm_record(), pool_record()],
        pending_pods=3,
    )
    assert report.pending_pods == 3
    assert added_up(report) == report.total


def test_a_record_keyed_on_the_resource_id_also_prices_the_node():
    # a provider that does not carry a `moref` dimension is matched on the resource id
    record = vm_record(moref="")
    report = split(mapped([node()], [target(uid="dc1/kube-1")]), [], [record])
    assert report.nodes_priced == 1
    assert report.total == Decimal("10.00")


def test_a_record_from_another_site_never_prices_this_cluster():
    report = split(mapped([node()]), [], [vm_record(alias="dc2"), pool_record()])
    assert report.unpriced_nodes == ("kube-1",)


def test_the_detector_warns_on_the_unrequested_share_and_skips_an_unpriced_cluster():
    report = split(mapped([node()]), [pod("apps", cpu="1", ram="4")], [vm_record(), pool_record()])
    events = NamespaceShowbackDetector(50.0, ("team",)).detect([report])
    assert events[0].severity is EventSeverity.WARN  # 60% of the iron nobody asked for
    assert "Namespace showback" in events[0].title
    assert events[0].payload["namespaces"] == {"apps": "2.00"}
    quiet = NamespaceShowbackDetector(90.0).detect([report])
    assert quiet[0].severity is EventSeverity.INFO
    unpriced = split(mapped([node()]), [], [pool_record()])
    assert NamespaceShowbackDetector().detect([unpriced]) == []
