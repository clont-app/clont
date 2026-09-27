"""NAT gateway processing a gateway endpoint would carry for free.

Pins the two ways this goes wrong: a vpc that already has both endpoints must not
be flagged, and a pending/deleted gateway is not a finding.
"""

from __future__ import annotations

from decimal import Decimal

from clont.finops.aws.nat_endpoints import NatGatewayEndpointCollector


class _Paginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **kw):
        yield from self._pages


class _FakeEC2:
    def __init__(self, nats: list[dict], endpoints: list[dict]) -> None:
        self._nats = nats
        self._endpoints = endpoints
        self.filters: list[dict] = []

    def get_paginator(self, name: str):
        if name == "describe_nat_gateways":
            return _Paginator([{"NatGateways": self._nats}])
        assert name == "describe_vpc_endpoints"
        outer = self

        class _P(_Paginator):
            def paginate(self, **kw):
                outer.filters = kw.get("Filters", [])
                yield {"VpcEndpoints": outer._endpoints}

        return _P([])


class _FakeProvider:
    def __init__(self, ec2: _FakeEC2) -> None:
        self._ec2 = ec2
        self.alias = "prod"

    def regions(self) -> list[str]:
        return ["us-east-1"]

    def client(self, service: str, region: str | None = None):
        assert service == "ec2"
        return self._ec2


def _nat(nat_id: str, vpc_id: str, state: str = "available") -> dict:
    return {"NatGatewayId": nat_id, "VpcId": vpc_id, "State": state}


def _endpoint(vpc_id: str, service: str) -> dict:
    return {"VpcId": vpc_id, "ServiceName": f"com.amazonaws.us-east-1.{service}"}


def _run(nats: list[dict], endpoints: list[dict]):
    ec2 = _FakeEC2(nats, endpoints)
    recs = NatGatewayEndpointCollector(_FakeProvider(ec2)).recommendations(None)
    return recs, ec2


def test_nat_without_gateway_endpoints_is_flagged():
    recs, ec2 = _run([_nat("nat-1", "vpc-1")], [])

    assert len(recs) == 1
    rec = recs[0]
    assert rec.kind == "nat-gateway-endpoint"
    assert rec.resource.resource_id == "vpc-1"
    assert "s3, dynamodb" in rec.summary and "nat-1" in rec.summary
    # no dollars invented: only cur knows how much of the bytes were s3/dynamodb
    assert rec.estimated_savings.amount == Decimal(0)
    # interface endpoints bill their own hourly charge, so only gateways count
    assert ec2.filters == [{"Name": "vpc-endpoint-type", "Values": ["Gateway"]}]


def test_only_the_missing_service_is_named():
    recs, _ = _run([_nat("nat-1", "vpc-1")], [_endpoint("vpc-1", "s3")])

    assert "dynamodb traffic" in recs[0].summary
    assert "s3," not in recs[0].summary


def test_a_vpc_with_both_endpoints_is_clean():
    recs, _ = _run(
        [_nat("nat-1", "vpc-1")],
        [_endpoint("vpc-1", "s3"), _endpoint("vpc-1", "dynamodb")],
    )

    assert recs == []


def test_endpoints_in_another_vpc_do_not_count():
    recs, _ = _run([_nat("nat-1", "vpc-1")], [_endpoint("vpc-2", "s3")])

    assert [r.resource.resource_id for r in recs] == ["vpc-1"]


def test_gateways_that_are_not_available_are_ignored():
    recs, _ = _run([_nat("nat-1", "vpc-1", state="deleted")], [])

    assert recs == []


def test_no_nat_gateway_means_the_endpoint_call_is_never_made():
    recs, ec2 = _run([], [])

    assert recs == []
    assert ec2.filters == []  # nothing to compare against, don't ask


def test_every_nat_in_the_vpc_is_named_once():
    recs, _ = _run([_nat("nat-2", "vpc-1"), _nat("nat-1", "vpc-1")], [])

    assert len(recs) == 1
    assert "nat-1, nat-2" in recs[0].summary
