"""eks leftovers: owned resources of a deleted cluster, nothing of a live one"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from clont.core.models import Period
from clont.finops.aws import pricing
from clont.finops.aws.eks_leftovers import EksLeftoversCollector, owner

REGION = "us-east-1"
PERIOD = Period(date(2026, 1, 1), date(2026, 1, 31))


class _Provider:
    alias = "prod"

    def regions(self) -> list[str]:
        return [REGION]

    def client(self, service: str, region: str | None = None):
        return boto3.client(service, region or REGION)


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        yield


def _tags(**kv: str) -> list[dict]:
    return [{"Key": k, "Value": v} for k, v in kv.items()]


def _volume(tags: dict[str, str]) -> str:
    ec2 = boto3.client("ec2", REGION)
    return ec2.create_volume(
        AvailabilityZone=f"{REGION}a", Size=10,
        TagSpecifications=[{"ResourceType": "volume", "Tags": _tags(**tags)}],
    )["VolumeId"]


def _subnets() -> list[str]:
    ec2 = boto3.client("ec2", REGION)
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    return [
        ec2.create_subnet(VpcId=vpc, CidrBlock=f"10.0.{n}.0/24",
                          AvailabilityZone=f"{REGION}{az}")["Subnet"]["SubnetId"]
        for n, az in ((1, "a"), (2, "b"))
    ]


def _classic(name: str, tags: dict[str, str]) -> None:
    boto3.client("elb", REGION).create_load_balancer(
        LoadBalancerName=name,
        Listeners=[{"Protocol": "tcp", "LoadBalancerPort": 80, "InstancePort": 30080}],
        AvailabilityZones=[f"{REGION}a"],
        Tags=_tags(**tags),
    )


def _recs():
    return EksLeftoversCollector(_Provider()).recommendations(PERIOD)


def test_owner_rules():
    assert owner({"kubernetes.io/cluster/old": "owned"}) == "old"
    assert owner({"kubernetes.io/cluster/old": "shared"}) is None
    assert owner({"elbv2.k8s.aws/cluster": "old"}) == "old"
    assert owner({"eks:cluster-name": "old"}) == "old"
    assert owner({"Name": "x"}) is None
    # two clusters on one resource, whose is it
    assert owner({"kubernetes.io/cluster/a": "owned", "eks:cluster-name": "b"}) is None


def test_dead_cluster_leftovers_collapse_into_one_finding(aws):
    vol = _volume({"kubernetes.io/cluster/old": "owned"})
    _classic("a1b2c3", {"kubernetes.io/cluster/old": "owned"})

    recs = _recs()

    assert [r.resource.resource_id for r in recs] == ["old"]
    rec = recs[0]
    assert rec.kind == "eks-leftover"
    assert vol in rec.summary and "already in unattached-ebs" in rec.summary
    assert "classic elb a1b2c3" in rec.summary
    # only the classic elb is priced, the volume is unattached-ebs's
    assert rec.estimated_savings.amount == pricing.load_balancer_quote(REGION).amount


def test_live_eks_cluster_is_not_a_leftover(aws):
    boto3.client("eks", REGION).create_cluster(
        name="live", roleArn="arn:aws:iam::123456789012:role/eks",
        resourcesVpcConfig={"subnetIds": _subnets()},
    )
    _volume({"kubernetes.io/cluster/live": "owned"})
    assert _recs() == []


def test_self_managed_cluster_with_running_nodes_is_live(aws):
    ec2 = boto3.client("ec2", REGION)
    ami = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    ec2.run_instances(
        ImageId=ami, MinCount=1, MaxCount=1,
        TagSpecifications=[{"ResourceType": "instance",
                            "Tags": _tags(**{"kubernetes.io/cluster/kops": "owned"})}],
    )
    _volume({"kubernetes.io/cluster/kops": "owned"})
    assert _recs() == []


def test_shared_and_attached_are_never_leftovers(aws):
    _volume({"kubernetes.io/cluster/old": "shared"})
    ec2 = boto3.client("ec2", REGION)
    ami = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    inst = ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1)["Instances"][0]
    vol = _volume({"kubernetes.io/cluster/old": "owned"})
    ec2.attach_volume(VolumeId=vol, InstanceId=inst["InstanceId"], Device="/dev/sdf")
    assert _recs() == []


def test_elastic_ip_named_not_priced(aws):
    ec2 = boto3.client("ec2", REGION)
    ip = ec2.allocate_address(
        Domain="vpc",
        TagSpecifications=[{"ResourceType": "elastic-ip",
                            "Tags": _tags(**{"eks:cluster-name": "old"})}],
    )["PublicIp"]
    recs = _recs()
    assert len(recs) == 1
    assert f"elastic ip {ip}, already in public ipv4" in recs[0].summary
    assert recs[0].estimated_savings.amount == Decimal(0)


def test_list_clusters_denied_means_no_findings(aws, monkeypatch):
    _volume({"kubernetes.io/cluster/old": "owned"})

    def denied(self):
        raise RuntimeError("AccessDenied")

    monkeypatch.setattr(EksLeftoversCollector, "_all_clusters", denied)
    assert _recs() == []


def test_elbv2_priced_only_when_it_still_has_targets(aws):
    subs = _subnets()
    elb = boto3.client("elbv2", REGION)
    owned = _tags(**{"elbv2.k8s.aws/cluster": "old"})
    busy = elb.create_load_balancer(Name="busy", Subnets=subs, Tags=owned)["LoadBalancers"][0]
    empty = elb.create_load_balancer(Name="empty", Subnets=subs, Tags=owned)["LoadBalancers"][0]
    vpc = boto3.client("ec2", REGION).describe_subnets(SubnetIds=subs)["Subnets"][0]["VpcId"]
    tg = elb.create_target_group(
        Name="tg", Protocol="HTTP", Port=80, VpcId=vpc, TargetType="ip"
    )["TargetGroups"][0]["TargetGroupArn"]
    elb.create_listener(
        LoadBalancerArn=busy["LoadBalancerArn"], Protocol="HTTP", Port=80,
        DefaultActions=[{"Type": "forward", "TargetGroupArn": tg}],
    )
    elb.register_targets(TargetGroupArn=tg, Targets=[{"Id": "10.0.1.10"}])
    # moto 404s an lb with no target group, real aws returns none
    tg2 = elb.create_target_group(
        Name="tg2", Protocol="HTTP", Port=80, VpcId=vpc, TargetType="ip"
    )["TargetGroups"][0]["TargetGroupArn"]
    elb.create_listener(
        LoadBalancerArn=empty["LoadBalancerArn"], Protocol="HTTP", Port=80,
        DefaultActions=[{"Type": "forward", "TargetGroupArn": tg2}],
    )

    rec, = _recs()
    assert "application lb empty, already in idle-elb" in rec.summary
    assert "application lb busy;" in rec.summary
    assert rec.estimated_savings.amount == pricing.load_balancer_quote(REGION).amount
