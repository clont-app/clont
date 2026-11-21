"""parity self-test: the gc engine must find exactly what today's ebs and eni checks find

ebs and eni add no findings of their own, a mismatch here is a gc bug
"""

from __future__ import annotations

from datetime import date

import boto3
import pytest
from moto import mock_aws

from clont.core.models import Period
from clont.finops.aws.public_ipv4 import PublicIPv4Collector
from clont.finops.aws.reachability import orphan_enis, orphan_volumes
from clont.finops.aws.waste import WasteCollector

REGION = "us-east-1"
PERIOD = Period(date(2026, 1, 1), date(2026, 1, 31))


class _Provider:
    alias = "prod"

    def __init__(self, ec2) -> None:
        self._ec2 = ec2

    def regions(self) -> list[str]:
        return [REGION]

    def client(self, service: str, region: str | None = None):
        return self._ec2


def _ids(recs, kind: str) -> set[str]:
    return {r.resource.resource_id for r in recs if r.kind == kind}


@pytest.fixture
def ec2(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        yield boto3.client("ec2", REGION)


def _instance(ec2, subnet: str) -> str:
    ami = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    return ec2.run_instances(
        ImageId=ami, MinCount=1, MaxCount=1, SubnetId=subnet
    )["Instances"][0]["InstanceId"]


def _subnet(ec2) -> tuple[str, str]:
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    sub = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24", AvailabilityZone=f"{REGION}a")
    return vpc, sub["Subnet"]["SubnetId"]


def test_volumes_match_unattached_ebs(ec2):
    _, sub = _subnet(ec2)
    running = _instance(ec2, sub)
    stopped = _instance(ec2, sub)
    ec2.stop_instances(InstanceIds=[stopped])

    def vol(size: int) -> str:
        return ec2.create_volume(Size=size, AvailabilityZone=f"{REGION}a", VolumeType="gp3")["VolumeId"]

    loose = vol(10)
    on_running = vol(20)
    on_stopped = vol(30)
    was_attached = vol(40)
    ec2.attach_volume(VolumeId=on_running, InstanceId=running, Device="/dev/sdf")
    ec2.attach_volume(VolumeId=on_stopped, InstanceId=stopped, Device="/dev/sdf")
    ec2.attach_volume(VolumeId=was_attached, InstanceId=running, Device="/dev/sdg")
    ec2.detach_volume(VolumeId=was_attached, InstanceId=running, Device="/dev/sdg")

    old = _ids(WasteCollector(_Provider(ec2)).recommendations(PERIOD), "unattached-ebs")
    gc = {v.volume_id for v in orphan_volumes(ec2)}
    assert old == gc == {loose, was_attached}


def test_enis_match_unattached_eni_ip_on_moto(ec2):
    _, sub = _subnet(ec2)
    inst = _instance(ec2, sub)

    def eni_with_eip() -> str:
        eni = ec2.create_network_interface(SubnetId=sub)["NetworkInterface"]["NetworkInterfaceId"]
        alloc = ec2.allocate_address(Domain="vpc")["AllocationId"]
        ec2.associate_address(AllocationId=alloc, NetworkInterfaceId=eni)
        return eni

    loose = eni_with_eip()
    attached = eni_with_eip()
    ec2.attach_network_interface(NetworkInterfaceId=attached, InstanceId=inst, DeviceIndex=1)
    ec2.create_network_interface(SubnetId=sub)  # no address, costs nothing

    old = _ids(PublicIPv4Collector(_Provider(ec2)).recommendations(PERIOD), "unattached-eni-ip")
    gc = {e.eni_id for e in orphan_enis(ec2)}
    assert old == gc == {loose}


class _Paginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **kw):
        yield from self._pages


class _FakeEC2:
    """shapes moto can't make: byoip, service-owned enis, a detached attachment"""

    def __init__(self, enis: list[dict], addresses: list[dict]) -> None:
        self._enis = enis
        self._addresses = addresses

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "describe_network_interfaces"
        return _Paginator([{"NetworkInterfaces": self._enis}])

    def describe_addresses(self, **kw) -> dict:
        return {"Addresses": self._addresses}


def _eni(eni_id: str, ip: str, status: str = "in-use", attachment: dict | None = None, **kw) -> dict:
    raw = {
        "NetworkInterfaceId": eni_id, "Status": status,
        "Association": {"PublicIp": ip},
        "PrivateIpAddresses": [{"Primary": True, "Association": {"PublicIp": ip}}],
        **kw,
    }
    if attachment is not None:
        raw["Attachment"] = attachment
    return raw


def test_enis_match_unattached_eni_ip_on_odd_shapes():
    enis = [
        _eni("eni-loose", "1.1.1.1", status="available"),
        _eni("eni-detached", "1.1.1.2", attachment={"AttachmentId": "a-1", "Status": "detached"}),
        _eni("eni-detaching", "1.1.1.3", attachment={"AttachmentId": "a-2", "Status": "detaching"}),
        # nat / lb enis: attached, but no instance id behind them
        _eni("eni-nat", "1.1.1.4", InterfaceType="nat_gateway",
             attachment={"AttachmentId": "a-3", "Status": "attached", "InstanceOwnerId": "amazon-aws"}),
        _eni("eni-byoip", "2.2.2.2", status="available"),
        _eni("eni-instance", "1.1.1.5", attachment={"AttachmentId": "a-4", "Status": "attached",
                                                    "InstanceId": "i-1"}),
        # only a secondary address, still billed
        {"NetworkInterfaceId": "eni-secondary", "Status": "available",
         "PrivateIpAddresses": [{"Primary": True},
                                {"Primary": False, "Association": {"PublicIp": "1.1.1.6"}}]},
    ]
    addresses = [{"PublicIp": "2.2.2.2", "AllocationId": "eipalloc-b", "PublicIpv4Pool": "ipv4pool-ec2-x"}]
    ec2 = _FakeEC2(enis, addresses)

    old = _ids(PublicIPv4Collector(_Provider(ec2)).recommendations(PERIOD), "unattached-eni-ip")
    gc = {e.eni_id for e in orphan_enis(ec2)}
    assert old == gc == {"eni-loose", "eni-detached", "eni-secondary"}
