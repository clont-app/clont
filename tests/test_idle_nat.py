"""Idle NAT gateway detection: ~zero bytes processed over the window."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from clont.finops.aws import pricing
from clont.finops.aws.idle_nat import IdleNatGatewayCollector
from clont.finops.base import FinOpsTuning

_TS = datetime(2026, 1, 1, tzinfo=UTC)


class _Paginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **kw):
        yield from self._pages


class _FakeEC2:
    def __init__(self, nats: list[dict]) -> None:
        self._nats = nats

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "describe_nat_gateways"
        return _Paginator([{"NatGateways": self._nats}])


class _FakeCW:
    def __init__(self, by_nat: dict[str, dict[str, list[float]]]) -> None:
        self.calls = 0
        self._by = by_nat

    def get_metric_data(self, MetricDataQueries, **kw) -> dict:
        self.calls += 1
        results = []
        for q in MetricDataQueries:
            metric = q["MetricStat"]["Metric"]["MetricName"]
            nat_id = q["MetricStat"]["Metric"]["Dimensions"][0]["Value"]
            values = self._by.get(nat_id, {}).get(metric, [])
            # real cloudwatch pairs every value with a timestamp
            results.append(
                {"Id": q["Id"], "Timestamps": [_TS] * len(values), "Values": values}
            )
        return {"MetricDataResults": results}


class _FakeProvider:
    def __init__(self, ec2: _FakeEC2, cw: _FakeCW, alias: str = "prod") -> None:
        self._clients = {"ec2": ec2, "cloudwatch": cw}
        self.alias = alias

    def regions(self) -> list[str]:
        return ["us-east-1"]

    def client(self, service: str, region: str | None = None):
        return self._clients[service]


def _nat(nat_id: str, state: str = "available") -> dict:
    return {"NatGatewayId": nat_id, "State": state}


# the cloudwatch path is an opt-in fallback now; compute optimizer is the default
_ON = FinOpsTuning(allow_cloudwatch_metrics=True)


def test_only_low_traffic_nat_gateways_are_flagged():
    cw = _FakeCW({
        "nat-idle": {"BytesOutToDestination": [0.0, 100.0], "BytesInFromSource": [0.0]},
        "nat-busy": {"BytesOutToDestination": [5_000_000.0], "BytesInFromSource": [0.0]},
    })
    ec2 = _FakeEC2([_nat("nat-idle"), _nat("nat-busy")])

    recs = IdleNatGatewayCollector(_FakeProvider(ec2, cw), _ON).recommendations(None)

    assert [r.resource.resource_id for r in recs] == ["nat-idle"]
    [rec] = recs
    assert rec.kind == "idle-nat"
    assert rec.estimated_savings.amount == pricing.NAT_GATEWAY_MONTH
    assert rec.estimated_savings.amount == Decimal("32.85")


def test_deleting_nat_gateways_are_skipped():
    cw = _FakeCW({"nat-idle": {"BytesOutToDestination": [0.0], "BytesInFromSource": [0.0]}})
    ec2 = _FakeEC2([_nat("nat-idle", state="deleting")])
    recs = IdleNatGatewayCollector(_FakeProvider(ec2, cw), _ON).recommendations(None)
    assert recs == []


def test_no_nat_gateways_no_recs():
    recs = IdleNatGatewayCollector(_FakeProvider(_FakeEC2([]), _FakeCW({})), _ON).recommendations(None)
    assert recs == []


def test_all_query_chunks_are_processed(monkeypatch):
    # With a tiny per-call limit, a busy gateway whose metrics land in a later
    # chunk must still be read (regression guard for the chunk loop).
    from clont.providers.aws import metrics

    monkeypatch.setattr(metrics, "_MAX_QUERIES", 2)  # 1 gateway (2 metrics) per call
    cw = _FakeCW({
        "nat-1": {"BytesOutToDestination": [0.0], "BytesInFromSource": [0.0]},
        "nat-2": {"BytesOutToDestination": [0.0], "BytesInFromSource": [0.0]},
        "nat-3": {"BytesOutToDestination": [9_000_000.0], "BytesInFromSource": [0.0]},  # busy, last chunk
    })
    ec2 = _FakeEC2([_nat("nat-1"), _nat("nat-2"), _nat("nat-3")])

    recs = IdleNatGatewayCollector(_FakeProvider(ec2, cw), _ON).recommendations(None)
    ids = sorted(r.resource.resource_id for r in recs)
    assert ids == ["nat-1", "nat-2"]  # nat-3 read from the 3rd chunk -> not idle


def test_cloudwatch_is_not_called_unless_the_operator_opts_in():
    cw = _FakeCW({"nat-idle": {"BytesOutToDestination": [0.0], "BytesInFromSource": [0.0]}})
    provider = _FakeProvider(_FakeEC2([_nat("nat-idle")]), cw)
    assert IdleNatGatewayCollector(provider).recommendations(None) == []
    assert cw.calls == 0
