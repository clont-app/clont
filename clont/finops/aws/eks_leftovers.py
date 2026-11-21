"""eks leftovers: billed resources a deleted cluster tagged as its own

a cluster is live if `eks:ListClusters` names it in any region, or if any
instance that isn't terminated still carries its tag (self-managed, kops).
only `owned` counts, `shared` (vpc, subnets) is never garbage. any failed read
drops the findings, a false "garbage" is worse than a miss.

volumes, elastic ips and empty alb/nlb are already priced by unattached-ebs,
public ipv4 and idle-elb, so here they are named, not priced again. a classic
elb and an alb/nlb that still has targets registered are priced here, no
other check covers them.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from clont.core.logging import get_logger
from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.aws import pricing
from clont.finops.aws.idle_elb import has_targets
from clont.finops.base import FinOpsTuning
from clont.finops.models import CostRecord, Recommendation
from clont.providers.aws.parsing import _EIP, _EBSVolume, _Instance, _LoadBalancer
from clont.providers.aws.regions import for_each_region
from clont.providers.base import Provider

log = get_logger("clont.finops.aws.eks_leftovers")

_KIND = "eks-leftover"
_USD = "USD"
_ELB_BATCH = 20  # describe_tags takes at most 20 lbs


@dataclass(frozen=True)
class ClusterTag:
    """how one controller marks what it made for a cluster"""

    key: str        # exact key, or a prefix when the name sits in the key
    in_key: bool    # name is the key suffix and the value is owned/shared


# controllers tag differently, add a rule here rather than a branch
CLUSTER_TAGS: tuple[ClusterTag, ...] = (
    ClusterTag("kubernetes.io/cluster/", in_key=True),    # in-tree provider, eksctl, kops
    ClusterTag("elbv2.k8s.aws/cluster", in_key=False),    # aws load balancer controller
    ClusterTag("eks:cluster-name", in_key=False),         # managed node groups, addons
)


def owner(tags: dict[str, str]) -> str | None:
    """the cluster that owns a resource, None if shared or untagged"""
    names: set[str] = set()
    for key, value in tags.items():
        for rule in CLUSTER_TAGS:
            if rule.in_key and key.startswith(rule.key) and key != rule.key:
                if value != "owned":
                    return None  # shared, or a value we don't know
                names.add(key[len(rule.key):])
            elif not rule.in_key and key == rule.key and value:
                names.add(value)
    # two clusters on one resource, can't say whose it is
    return names.pop() if len(names) == 1 else None


def tagged_clusters(tags: dict[str, str]) -> set[str]:
    """every cluster a resource names, owned or shared"""
    out: set[str] = set()
    for key, value in tags.items():
        for rule in CLUSTER_TAGS:
            if rule.in_key and key.startswith(rule.key) and key != rule.key:
                out.add(key[len(rule.key):])
            elif not rule.in_key and key == rule.key and value:
                out.add(value)
    return out


@dataclass(frozen=True)
class Leftover:
    what: str           # "volume", "classic elb", ...
    rid: str
    claimed_by: str     # the check that already prices it, "" = priced here
    monthly: Decimal = Decimal(0)
    approximate: bool = False


@dataclass
class RegionScan:
    leftovers: dict[str, list[Leftover]] = field(default_factory=lambda: defaultdict(list))
    live: set[str] = field(default_factory=set)  # clusters a running instance still names


def _pairs(tags: list[dict]) -> dict[str, str]:
    return {t.get("Key", ""): t.get("Value", "") for t in tags}


def scan_region(provider: Provider, region: str) -> RegionScan:
    """every owned, billed resource in the region, grouped by cluster"""
    scan = RegionScan()
    ec2 = provider.client("ec2", region)

    pages = ec2.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "instance-state-name",
                  "Values": ["pending", "running", "shutting-down", "stopping", "stopped"]}]
    )
    for page in pages:
        for res in page.get("Reservations", []):
            for raw in res.get("Instances", []):
                scan.live |= tagged_clusters(_Instance.model_validate(raw).tag_map())

    for page in ec2.get_paginator("describe_volumes").paginate():
        for raw in page.get("Volumes", []):
            vol = _EBSVolume.model_validate(raw)
            name = owner(vol.tag_map())
            # attached means something still holds it
            if name and vol.state == "available":
                scan.leftovers[name].append(Leftover("volume", vol.volume_id, "unattached-ebs"))

    for raw in ec2.describe_addresses().get("Addresses", []):
        eip = _EIP.model_validate(raw)
        name = owner(eip.tag_map())
        if name and not eip.association_id:
            claim = "" if eip.byoip else "public ipv4"
            scan.leftovers[name].append(
                Leftover("elastic ip", eip.public_ip or eip.allocation_id, claim)
            )

    _elbv2(provider, region, scan)
    _classic(provider, region, scan)
    return scan


def _elbv2(provider: Provider, region: str, scan: RegionScan) -> None:
    elb = provider.client("elbv2", region)
    lbs: dict[str, _LoadBalancer] = {}
    for page in elb.get_paginator("describe_load_balancers").paginate():
        for raw in page.get("LoadBalancers", []):
            lb = _LoadBalancer.model_validate(raw)
            if lb.arn and (not lb.state or lb.state == "active"):
                lbs[lb.arn] = lb
    arns = list(lbs)
    for batch in (arns[i : i + _ELB_BATCH] for i in range(0, len(arns), _ELB_BATCH)):
        for desc in elb.describe_tags(ResourceArns=batch).get("TagDescriptions", []):
            name = owner(_pairs(desc.get("Tags", [])))
            lb = lbs.get(desc.get("ResourceArn", ""))
            if not name or lb is None:
                continue
            what = f"{lb.lb_type or 'elbv2'} lb"
            if has_targets(elb, lb.arn):
                # targets of a dead cluster, idle-elb won't flag it
                q = pricing.load_balancer_quote(region)
                scan.leftovers[name].append(Leftover(what, lb.name, "", q.amount, q.approximate))
            else:
                scan.leftovers[name].append(Leftover(what, lb.name, "idle-elb"))


def _classic(provider: Provider, region: str, scan: RegionScan) -> None:
    elb = provider.client("elb", region)
    names: list[str] = []
    for page in elb.get_paginator("describe_load_balancers").paginate():
        names.extend(d["LoadBalancerName"] for d in page.get("LoadBalancerDescriptions", []))
    for batch in (names[i : i + _ELB_BATCH] for i in range(0, len(names), _ELB_BATCH)):
        for desc in elb.describe_tags(LoadBalancerNames=batch).get("TagDescriptions", []):
            name = owner(_pairs(desc.get("Tags", [])))
            if name:
                # the in-tree default for a LoadBalancer service, nobody else checks it
                q = pricing.load_balancer_quote(region)
                scan.leftovers[name].append(Leftover(
                    "classic elb", desc.get("LoadBalancerName", ""), "", q.amount, q.approximate
                ))


@register("finops", Cloud.AWS, "eks_leftovers")
class EksLeftoversCollector:
    cloud = Cloud.AWS
    service = "eks_leftovers"

    def __init__(self, provider: Provider, tuning: FinOpsTuning | None = None) -> None:
        self._provider = provider
        self._clusters: set[str] | None = None

    def collect(self, period: Period) -> list[CostRecord]:
        return []

    def recommendations(self, period: Period) -> list[Recommendation]:
        try:
            self._clusters = self._all_clusters()
        except Exception as exc:  # noqa: BLE001 - can't tell live from dead
            log.warning(
                "eks leftovers: eks:ListClusters failed for %s — add it to the role; "
                "skipping the check: %s", self._provider.alias, exc,
            )
            return []
        return for_each_region(self._provider, self._region, what="eks leftovers")

    def _all_clusters(self) -> set[str]:
        # a tag can name a cluster from another region, so look everywhere
        out: set[str] = set()
        for region in self._provider.regions():
            eks = self._provider.client("eks", region)
            for page in eks.get_paginator("list_clusters").paginate():
                out.update(page.get("clusters", []))
        return out

    def _region(self, region: str) -> list[Recommendation]:
        try:
            scan = scan_region(self._provider, region)
        except Exception as exc:  # noqa: BLE001 - unknown consumer means live
            log.warning(
                "eks leftovers: read failed in %s for %s, skipping the region: %s",
                region, self._provider.alias, exc,
            )
            return []
        live = (self._clusters or set()) | scan.live
        return [
            self._rec(name, items, region)
            for name, items in sorted(scan.leftovers.items())
            if name not in live
        ]

    def _rec(self, cluster: str, items: list[Leftover], region: str) -> Recommendation:
        priced = [i for i in items if not i.claimed_by]
        return Recommendation(
            cloud=str(Cloud.AWS),
            service="eks",
            kind=_KIND,
            resource=CloudResource(
                cloud=Cloud.AWS,
                service="eks",
                resource_id=cluster,
                region=region,
                alias=self._provider.alias,
            ),
            summary=summary(cluster, items),
            estimated_savings=Money(amount=sum((i.monthly for i in priced), Decimal(0)), currency=_USD),
            approximate=any(i.approximate for i in priced),
        )


def summary(cluster: str, items: list[Leftover]) -> str:
    parts: list[str] = []
    for i in sorted(items, key=lambda i: (i.what, i.rid)):
        note = f", already in {i.claimed_by}" if i.claimed_by else ""
        parts.append(f"{i.what} {i.rid}{note}")
    return (
        f"EKS cluster {cluster} is gone: not in eks:ListClusters in any region and no "
        f"instance carries its tag, but it left {len(items)} resource"
        f"{'' if len(items) == 1 else 's'} tagged as its own: {'; '.join(parts)} — delete them"
    )
