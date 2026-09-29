"""Waste recommendations: unattached EBS, gp2->gp3. EIPs moved to public_ipv4."""

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
    assert set(by_id) == {"vol-free", "vol-gp2"}  # in-use gp3 skipped
    # EIPs belong to public_ipv4 now; flagging them here too bills $3.65 twice
    assert not [r for r in recs if r.kind == "unassociated-eip"]

    assert by_id["vol-free"].estimated_savings.amount == Decimal("8.00")   # 100 GiB * $0.08
    assert by_id["vol-free"].kind == "unattached-ebs"
    assert "Unattached" in by_id["vol-free"].summary
    # 50 GiB * $0.02, less the 3 MiBps gp3 must buy to match gp2's 128
    assert by_id["vol-gp2"].estimated_savings.amount == Decimal("0.88")
    assert by_id["vol-gp2"].kind == "gp2-gp3"
    assert "gp3" in by_id["vol-gp2"].summary


def test_unattached_volume_is_priced_with_its_provisioned_performance():
    # 200 GiB gp3 at 16k iops / 500 MiBps: the performance costs 5x the storage
    volumes = [{
        "VolumeId": "vol-fast", "Size": 200, "VolumeType": "gp3", "State": "available",
        "Iops": 16000, "Throughput": 500,
    }]
    recs = WasteCollector(_FakeProvider(_FakeEC2(volumes, []))).recommendations(None)
    storage = Decimal("0.08") * 200
    extra = Decimal("0.005") * 13000 + Decimal("0.04") * 375
    assert recs[0].estimated_savings.amount == storage + extra


def test_gp2_migration_saving_is_net_of_the_iops_it_has_to_buy():
    # 16 TiB: gp2 gives 16000 iops free, gp3 bills 13000 of them
    volumes = [{"VolumeId": "vol-big", "Size": 16384, "VolumeType": "gp2", "State": "in-use"}]
    recs = WasteCollector(_FakeProvider(_FakeEC2(volumes, []))).recommendations(None)
    storage_delta = Decimal("0.02") * 16384
    parity = Decimal("0.005") * 13000 + Decimal("0.04") * 125
    assert recs[0].estimated_savings.amount == storage_delta - parity


def test_gp2_migration_is_not_advised_when_parity_costs_more():
    # reported iops above the size baseline (a converted volume) can outrun the
    # storage saving; advising a negative number is worse than staying quiet
    volumes = [{
        "VolumeId": "vol-odd", "Size": 100, "VolumeType": "gp2", "State": "in-use",
        "Iops": 16000,
    }]
    recs = WasteCollector(_FakeProvider(_FakeEC2(volumes, []))).recommendations(None)
    assert recs == []


def test_waste_empty_when_nothing_wasteful():
    volumes = [{"VolumeId": "vol-fine", "Size": 8, "VolumeType": "gp3", "State": "in-use"}]
    addresses = [{"PublicIp": "5.6.7.8", "AllocationId": "a", "AssociationId": "b"}]
    recs = WasteCollector(_FakeProvider(_FakeEC2(volumes, addresses))).recommendations(None)
    assert recs == []
