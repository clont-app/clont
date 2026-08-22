"""Idle EC2 detection: low CPU + low network over the window -> recommendation."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from clont.events.detectors import RecommendationDetector
from clont.finops.aws.idle import IdleCollector
from clont.finops.base import FinOpsTuning

_TS = datetime(2026, 1, 1, tzinfo=UTC)


class _Paginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **kw):
        yield from self._pages


class _FakeEC2:
    def __init__(self, ids: list[str]) -> None:
        self._ids = ids

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "describe_instance_status"
        return _Paginator([{"InstanceStatuses": [{"InstanceId": i} for i in self._ids]}])


class _FakeCW:
    def __init__(self, by_instance: dict[str, dict[str, list[float]]]) -> None:
        self.calls = 0
        self._by = by_instance

    def get_metric_data(self, MetricDataQueries, **kw) -> dict:
        self.calls += 1
        results = []
        for q in MetricDataQueries:
            metric = q["MetricStat"]["Metric"]["MetricName"]
            iid = q["MetricStat"]["Metric"]["Dimensions"][0]["Value"]
            values = self._by.get(iid, {}).get(metric, [])
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


# the cloudwatch path is an opt-in fallback now; compute optimizer is the default
_ON = FinOpsTuning(allow_cloudwatch_metrics=True)


def _metrics(cpu, net_in=0.0, net_out=0.0):
    return {"CPUUtilization": cpu, "NetworkIn": [net_in], "NetworkOut": [net_out]}


def test_only_low_cpu_low_network_instances_are_flagged():
    cw = _FakeCW({
        "i-idle": _metrics([1.0, 2.0], net_in=1000, net_out=1000),     # idle
        "i-busy-cpu": _metrics([50.0], net_in=1000, net_out=1000),     # CPU busy
        "i-busy-net": _metrics([1.0], net_in=10_000_000),              # network busy
        "i-nodata": {"CPUUtilization": [], "NetworkIn": [], "NetworkOut": []},  # no metrics
    })
    ec2 = _FakeEC2(["i-idle", "i-busy-cpu", "i-busy-net", "i-nodata"])

    recs = IdleCollector(_FakeProvider(ec2, cw), _ON).recommendations(None)

    assert [r.resource.resource_id for r in recs] == ["i-idle"]
    [rec] = recs
    assert rec.service == "ec2"
    assert rec.estimated_savings.amount == Decimal(0)  # unknown (no per-instance price)
    assert "Idle" in rec.summary


def test_idle_event_has_no_savings_clause():
    cw = _FakeCW({"i-idle": _metrics([0.5], net_in=10, net_out=10)})
    recs = IdleCollector(_FakeProvider(_FakeEC2(["i-idle"]), cw), _ON).recommendations(None)
    [event] = RecommendationDetector().detect(recs)
    assert "est. savings" not in event.message  # 0 = unknown -> clause omitted
    assert event.key == "finops:rec:prod:aws:ec2:idle:i-idle"


def test_no_instances_no_calls():
    recs = IdleCollector(_FakeProvider(_FakeEC2([]), _FakeCW({})), _ON).recommendations(None)
    assert recs == []


def test_tuning_raises_cpu_threshold():
    # A box at 30% CPU is not idle under the default 5% threshold, but is when
    # the operator raises the threshold to 50% via config.
    cw = _FakeCW({"i-mid": _metrics([30.0], net_in=10, net_out=10)})
    provider = _FakeProvider(_FakeEC2(["i-mid"]), cw)

    assert IdleCollector(provider, _ON).recommendations(None) == []

    tuned = IdleCollector(provider, FinOpsTuning(idle_cpu_pct=50.0, allow_cloudwatch_metrics=True))
    [rec] = tuned.recommendations(None)
    assert rec.resource.resource_id == "i-mid"


def test_cloudwatch_is_not_called_unless_the_operator_opts_in():
    """Default: compute optimizer covers idle ec2, so no billed GetMetricData."""
    cw = _FakeCW({"i-idle": _metrics([1.0], net_in=1.0, net_out=1.0)})
    provider = _FakeProvider(_FakeEC2(["i-idle"]), cw)
    assert IdleCollector(provider).recommendations(None) == []
    assert cw.calls == 0
