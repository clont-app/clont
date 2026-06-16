"""Waste recommendations: unattached EBS, unassociated EIP, gp2->gp3."""

from __future__ import annotations

from decimal import Decimal

from clont.finops.aws.waste import WasteCollector


class _Paginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **kw):
        yield from self._pages


class _FakeEC2:
    def __init__(self, volumes: list[dict], addresses: list[dict]) -> None:
        self._volumes = volumes
        self._addresses = addresses

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "describe_volumes"
        return _Paginator([{"Volumes": self._volumes}])

    def describe_addresses(self, **kw) -> dict:
        return {"Addresses": self._addresses}


class _FakeProvider:
    def __init__(self, ec2: _FakeEC2, alias: str = "prod") -> None:
        self._ec2 = ec2
        self.alias = alias

    def regions(self) -> list[str]:
        return ["us-east-1"]

    def client(self, service: str, region: str | None = None):
        assert service == "ec2"
        return self._ec2


def test_waste_detects_each_kind_and_prices_them():
    volumes = [
        {"VolumeId": "vol-free", "Size": 100, "VolumeType": "gp3", "State": "available"},
        {"VolumeId": "vol-gp2", "Size": 50, "VolumeType": "gp2", "State": "in-use"},
        {"VolumeId": "vol-fine", "Size": 200, "VolumeType": "gp3", "State": "in-use"},
    ]
    addresses = [
        {"PublicIp": "1.2.3.4", "AllocationId": "eipalloc-idle"},          # unassociated
        {"PublicIp": "5.6.7.8", "AllocationId": "eipalloc-used", "AssociationId": "eipassoc-1"},
    ]
    recs = WasteCollector(_FakeProvider(_FakeEC2(volumes, addresses))).recommendations(None)

    by_id = {r.resource.resource_id: r for r in recs}
    assert set(by_id) == {"vol-free", "vol-gp2", "eipalloc-idle"}  # in-use gp3 + used EIP skipped

    assert by_id["vol-free"].estimated_savings.amount == Decimal("8.00")   # 100 GiB * $0.08
    assert by_id["vol-free"].kind == "unattached-ebs"
    assert "Unattached" in by_id["vol-free"].summary
    assert by_id["vol-gp2"].estimated_savings.amount == Decimal("1.00")    # 50 GiB * $0.02
    assert by_id["vol-gp2"].kind == "gp2-gp3"
    assert "gp3" in by_id["vol-gp2"].summary
    assert by_id["eipalloc-idle"].estimated_savings.amount == Decimal("3.60")
    assert by_id["eipalloc-idle"].kind == "unassociated-eip"


def test_waste_empty_when_nothing_wasteful():
    volumes = [{"VolumeId": "vol-fine", "Size": 8, "VolumeType": "gp3", "State": "in-use"}]
    addresses = [{"PublicIp": "5.6.7.8", "AllocationId": "a", "AssociationId": "b"}]
    recs = WasteCollector(_FakeProvider(_FakeEC2(volumes, addresses))).recommendations(None)
    assert recs == []
