"""`unmounted-pvc` / `abandoned-namespace`: the claims that outlived their pods.

The price chain is the operator's card: the pool line publishes `rate_storage_gib_month`,
and that is what a claim's GiB are multiplied by — nothing here invents a $/GiB. With no
card the finding is still made, at 0.00, because a 100 GiB volume nothing mounts is worth
saying out loud on an unpriced cluster too.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from clont.core.models import Cloud, CloudResource, Money, Period
from clont.finops.base import FinOpsTuning
from clont.finops.k8s.mapping import Priced, match
from clont.finops.k8s.source import ClusterRead, KubernetesSource
from clont.finops.k8s.volumes import ABANDONED, UNMOUNTED, reclaim
from clont.finops.models import CostRecord
from clont.providers.k8s.config import KubernetesCluster
from clont.providers.k8s.nodes import Node
from clont.providers.k8s.pods import Pod, build_pods
from clont.providers.k8s.volumes import Claim, build_claims
from clont.providers.onprem.inventory import Pool, SiteInventory, Vm

DAY = date(2026, 10, 6)
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
MOREF = "vim.VirtualMachine:vm-1"
# the card puts 20% of a 36,500.00/month pool on 91,250 GiB of array: 0.08 $/GiB-month
RATE = Decimal("0.08")


def node() -> Node:
    return Node(
        name="kube-1",
        uid="uid-1",
        vcpu=Decimal(4),
        ram_gib=Decimal(16),
        allocatable_vcpu=Decimal(4),
        allocatable_ram_gib=Decimal(16),
    )


def claim(
    name: str,
    *,
    ns: str = "apps",
    gib: str = "100",
    phase: str = "Bound",
    age_days: int = 30,
    storage_class: str = "vsphere-csi",
) -> Claim:
    return Claim(
        namespace=ns,
        name=name,
        gib=Decimal(gib),
        phase=phase,
        storage_class=storage_class,
        volume=f"pv-{name}",
        created=NOW - timedelta(days=age_days),
    )


def pod(name: str, *, ns: str = "apps", claims=(), node_name: str = "kube-1") -> Pod:
    return Pod(namespace=ns, name=name, node=node_name, claims=tuple(claims))


def target() -> Priced:
    return Priced(kind="vm", uid=MOREF, name="kube-1", alias="dc1", pool="prod-gen11")


def vm_record() -> CostRecord:
    return CostRecord(
        cloud=str(Cloud.ONPREM),
        service="vm",
        period=Period(start=DAY, end=DAY),
        alias="dc1",
        cost=Money(amount=Decimal("24.00"), currency="USD"),
        resource=CloudResource(
            cloud=Cloud.ONPREM, service="vm", resource_id="dc1/kube-1", alias="dc1"
        ),
        dimensions={"cluster": "prod-gen11", "moref": MOREF},
    )


def pool_record(storage: str = "0.08") -> CostRecord:
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
        },
    )


def run(claims, pods=(), pending=(), *, records=None, **tune):
    mapping = match("lab", [node()], [target()])
    return reclaim(
        mapping,
        list(pods),
        list(pending),
        list(claims),
        records if records is not None else [vm_record(), pool_record()],
        tuning=FinOpsTuning(**tune),
        now=NOW,
    )


def test_a_bound_claim_nothing_mounts_is_priced_off_the_pool_card():
    report = run([claim("data")], pods=[pod("web")])
    finding = report.findings[0]
    assert (finding.kind, finding.ref, finding.region) == (UNMOUNTED, "apps/data", "dc1/prod-gen11")
    assert finding.monthly == Decimal(100) * RATE == Decimal("8.00")
    assert finding.gib == Decimal("100.0")
    assert "30+ days old" in finding.summary
    assert "vsphere-csi" in finding.summary
    # the honest half: data, and what the hypervisor could see of it
    assert "deleting it deletes the data" in finding.summary
    assert "unaccounted datastore space" in finding.summary
    assert report.gib_month == RATE


def test_a_mounted_claim_is_not_a_finding():
    assert run([claim("data")], pods=[pod("web", claims=["data"])]).findings == ()


def test_a_pending_pod_mounting_it_counts_as_use():
    # the pod is unschedulable *because* of this volume: the namespace is anything but idle
    report = run([claim("data")], pending=[pod("web", claims=["data"], node_name="")])
    assert report.findings == ()


def test_a_claim_of_the_same_name_in_another_namespace_does_not_cover_it():
    pods = [pod("web", ns="other", claims=["data"]), pod("api")]
    report = run([claim("data")], pods=pods)
    assert [f.ref for f in report.findings] == ["apps/data"]


def test_an_empty_namespace_is_one_finding_not_one_per_claim():
    report = run([claim("data"), claim("logs", gib="50")], pods=[pod("web", ns="other")])
    assert [(f.kind, f.ref) for f in report.findings] == [(ABANDONED, "apps")]
    finding = report.findings[0]
    assert finding.gib == Decimal("150.0")
    assert finding.monthly == Decimal("12.00")
    assert "data, logs" in finding.summary
    assert "only runs cronjobs" in finding.summary  # the false positive, named


def test_a_namespace_with_one_live_pod_is_not_abandoned():
    report = run([claim("data"), claim("logs", gib="50")], pods=[pod("web")])
    assert [f.ref for f in report.findings] == ["apps/data", "apps/logs"]


def test_a_young_claim_is_a_deploy_in_progress():
    report = run([claim("data", age_days=1)], pods=[pod("web")])
    assert report.findings == ()
    # it is still counted as unmounted: the gate is about advice, not about the number
    assert report.unmounted_gib == Decimal("100.0")


def test_a_claim_with_no_timestamp_passes_and_says_so():
    rows = [Claim(namespace="apps", name="data", gib=Decimal(100), phase="Bound")]
    report = run(rows, pods=[pod("web")])
    assert "age unknown" in report.findings[0].summary


def test_a_pending_claim_holds_no_blocks():
    report = run([claim("data", phase="Pending")], pods=[pod("web")])
    assert report.findings == ()
    assert report.claims == 1


def test_a_small_claim_is_noise():
    report = run([claim("data", gib="2")], pods=[pod("web")])
    assert report.findings == ()


def test_a_cluster_with_no_card_still_reports_the_capacity_unpriced():
    report = run([claim("data")], pods=[pod("web")], records=[vm_record()])
    finding = report.findings[0]
    assert finding.monthly == Decimal(0)
    assert "publishes no $/GiB-month" in finding.summary
    assert "unpriced" in report.summary()


def test_the_floor_only_applies_once_there_is_a_price():
    priced = run([claim("data")], pods=[pod("web")], onprem_min_savings_usd=100.0)
    assert priced.findings == ()
    unpriced = run(
        [claim("data")], pods=[pod("web")], records=[vm_record()], onprem_min_savings_usd=100.0
    )
    assert len(unpriced.findings) == 1


def test_the_biggest_claim_is_reported_first():
    report = run(
        [claim("data", gib="100"), claim("logs", gib="500")], pods=[pod("web")]
    )
    assert [f.ref for f in report.findings] == ["apps/logs", "apps/data"]


# --- the reader: what the api says, as clont reads it ---


def test_the_capacity_given_wins_over_the_capacity_asked_for():
    rows = build_claims(
        [
            {
                "metadata": {"namespace": "apps", "name": "data", "creationTimestamp": "2026-09-01T00:00:00Z"},
                "spec": {
                    "resources": {"requests": {"storage": "7Gi"}},
                    "storageClassName": "local-path",
                    "volumeName": "pv-1",
                },
                "status": {"phase": "Bound", "capacity": {"storage": "10Gi"}},
            }
        ]
    )
    assert rows[0].gib == Decimal(10)  # the provisioner cut 10, the claim asked for 7
    assert rows[0].volume == "pv-1"
    assert rows[0].bound is True
    assert rows[0].created == datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_a_pending_claim_falls_back_to_its_request():
    rows = build_claims(
        [
            {
                "metadata": {"namespace": "apps", "name": "data"},
                "spec": {"resources": {"requests": {"storage": "5Gi"}}},
                "status": {"phase": "Pending"},
            }
        ]
    )
    assert (rows[0].gib, rows[0].bound, rows[0].created) == (Decimal(5), False, None)


def test_a_pod_names_the_claims_it_mounts_including_an_ephemeral_one():
    pods, _ = build_pods(
        [
            {
                "metadata": {"namespace": "apps", "name": "web-1"},
                "spec": {
                    "nodeName": "kube-1",
                    "containers": [],
                    "volumes": [
                        {"name": "data", "persistentVolumeClaim": {"claimName": "data"}},
                        {"name": "scratch", "ephemeral": {"volumeClaimTemplate": {}}},
                        {"name": "config", "configMap": {"name": "c"}},
                    ],
                },
            }
        ]
    )
    # the ephemeral one is named <pod>-<volume> by the controller, and it is mounted
    assert pods[0].claims == ("data", "web-1-scratch")


# --- the source end ---


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


def test_the_volume_finding_comes_out_as_a_recommendation():
    read = ClusterRead(
        nodes=[node()],
        pods=[pod("web", ns="other")],
        claims=[claim("data")],
    )
    src = KubernetesSource(
        "lab", KubernetesCluster(priced_by="dc1", usage="off"), _Site(), reader=lambda: read
    )
    recs = src.recommendations([vm_record(), pool_record()])
    assert [(r.kind, r.resource.resource_id) for r in recs] == [(ABANDONED, "apps")]
    assert recs[0].estimated_savings.amount == Decimal("8.00")
    assert recs[0].resource.alias == "lab"
    assert recs[0].approximate is True
