"""Collector cadence: the loop ticks every 300s, the paid APIs must not.

The load-bearing property isn't "fewer calls" — it's that a *reused* result is
indistinguishable downstream from a fresh one. A collector that goes quiet
between refreshes would empty `batch.costs` on 287 of 288 cycles and the spend
digest would silently vanish.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

from clont.agent import runner as runner_mod
from clont.agent.cadence import Due
from clont.agent.runner import Agent
from clont.core.models import Cloud, CloudResource, Money, Period
from clont.finops.models import CostRecord, Recommendation
from clont.monitoring.base import MetricsPolicy
from clont.monitoring.models import MetricPoint


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _record() -> CostRecord:
    return CostRecord(
        cloud=str(Cloud.AWS),
        service="ec2",
        period=Period(start=date(2026, 8, 1), end=date(2026, 8, 2)),
        cost=Money(amount=Decimal("12.50")),
        alias="prod",
    )


def _rec() -> Recommendation:
    return Recommendation(
        cloud=str(Cloud.AWS),
        service="ec2",
        kind="idle",
        resource=CloudResource(cloud=Cloud.AWS, service="ec2", resource_id="i-1", alias="prod"),
        summary="idle",
        estimated_savings=Money(amount=Decimal("61")),
    )


class _Counting:
    """Counts how often it actually reached the (imaginary) API."""

    collects = 0
    recommends = 0

    def __init__(self, *args) -> None: ...

    def collect(self, period):
        type(self).collects += 1
        return [_record()]

    def recommendations(self, period):
        type(self).recommends += 1
        return [_rec()]


def _counting_collector(**attrs):
    return type("_Counting", (_Counting,), {"collects": 0, "recommends": 0, **attrs})


def _agent(monkeypatch, cls, clock, **kwargs) -> Agent:
    monkeypatch.setattr(
        runner_mod.registry,
        "collectors_for",
        lambda kind, cloud: [cls] if kind == "finops" else [],
    )
    return Agent(
        [SimpleNamespace(cloud=Cloud.AWS, alias="prod")], [], clock=clock, **kwargs
    )


def test_inside_the_ttl_the_collector_is_called_once(monkeypatch):
    clock = _Clock()
    cls = _counting_collector()
    agent = _agent(monkeypatch, cls, clock, collect_interval_seconds=86400)

    for _ in range(10):
        agent._collect_batch()
        clock.advance(300)

    assert cls.collects == 1


def test_reused_records_still_reach_the_batch_and_the_detectors(monkeypatch):
    # the trap: reuse, don't skip. a cycle that ships no costs makes the digest
    # disappear without anything looking broken.
    clock = _Clock()
    cls = _counting_collector()
    agent = _agent(monkeypatch, cls, clock, collect_interval_seconds=86400)

    first = agent._collect_batch()
    clock.advance(300)
    second = agent._collect_batch()

    assert cls.collects == 1
    assert len(second.costs) == len(first.costs) == 1
    assert second.costs[0].cost.amount == Decimal("12.50")
    assert second.events  # the digest still fires off the reused records


def test_the_ttl_expiring_refreshes(monkeypatch):
    clock = _Clock()
    cls = _counting_collector()
    agent = _agent(monkeypatch, cls, clock, collect_interval_seconds=3600)

    agent._collect_batch()
    clock.advance(3601)
    agent._collect_batch()

    assert cls.collects == 2


def test_force_bypasses_the_cache(monkeypatch):
    # `clont run --summary` is the ad-hoc scan: it must see today's numbers.
    clock = _Clock()
    cls = _counting_collector()
    agent = _agent(monkeypatch, cls, clock, collect_interval_seconds=86400)

    agent._collect_batch()
    agent._collect_batch(force=True)

    assert cls.collects == 2


def test_a_class_attr_beats_the_operator_default(monkeypatch):
    # CURCostCollector is free and self-throttled, so it opts into a shorter ttl
    clock = _Clock()
    cls = _counting_collector(collect_every_seconds=3600)
    agent = _agent(monkeypatch, cls, clock, collect_interval_seconds=86400)

    agent._collect_batch()
    clock.advance(3601)
    agent._collect_batch()

    assert cls.collects == 2


def test_a_failed_refresh_keeps_the_last_good_value(monkeypatch):
    # one throttled call must not blank the spend for the rest of the day
    clock = _Clock()

    class _FlakyThenBroken(_Counting):
        collects = 0
        recommends = 0

        def collect(self, period):
            type(self).collects += 1
            if type(self).collects > 1:
                raise RuntimeError("ThrottlingException")
            return [_record()]

    agent = _agent(monkeypatch, _FlakyThenBroken, clock, collect_interval_seconds=3600)

    agent._collect_batch()
    clock.advance(3601)
    batch = agent._collect_batch()

    assert len(batch.costs) == 1                    # last good value, not nothing
    assert any("ThrottlingException" in e for e in batch.errors)  # but still loud


def test_recommendations_have_their_own_cadence(monkeypatch):
    clock = _Clock()
    cls = _counting_collector()
    agent = _agent(
        monkeypatch,
        cls,
        clock,
        collect_interval_seconds=86400,
        recommend_interval_seconds=3600,
    )

    agent._collect_batch()
    clock.advance(3601)
    batch = agent._collect_batch()

    assert (cls.collects, cls.recommends) == (1, 2)
    assert len(batch.recommendations) == 1


class _CountingMetrics:
    cloud = Cloud.AWS
    service = "ec2"
    collects = 0

    def __init__(self, provider, metrics=None) -> None:
        self._metrics = metrics

    def health(self):
        return []

    def collect(self, period):
        type(self).collects += 1
        self._metrics.trim([_query()])  # a real read spends the cycle's budget
        return [
            MetricPoint(
                name="CPUUtilization",
                value=1.0,
                unit="Percent",
                timestamp=datetime(2026, 8, 1, tzinfo=UTC),
                resource=CloudResource(
                    cloud=Cloud.AWS, service="ec2", resource_id="i-1", alias="prod"
                ),
            )
        ]


def _query() -> dict:
    return {"MetricStat": {"Metric": {"MetricName": "CPUUtilization"}, "Period": 300}}


def _metrics_agent(monkeypatch, clock, **kwargs) -> tuple[Agent, MetricsPolicy]:
    _CountingMetrics.collects = 0
    monkeypatch.setattr(
        runner_mod.registry,
        "collectors_for",
        lambda kind, cloud: [_CountingMetrics] if kind == "monitoring" else [],
    )
    policy = MetricsPolicy(max_per_cycle=1000)
    agent = Agent(
        [SimpleNamespace(cloud=Cloud.AWS, alias="prod")],
        [],
        clock=clock,
        metrics=policy,
        **kwargs,
    )
    return agent, policy


def test_metrics_default_to_every_cycle(monkeypatch):
    # None keeps the pre-cadence behaviour, so an upgrade changes nothing
    clock = _Clock()
    agent, _ = _metrics_agent(monkeypatch, clock)

    agent._collect_batch()
    clock.advance(300)
    agent._collect_batch()

    assert _CountingMetrics.collects == 2


def test_a_reused_series_spends_no_metric_budget(monkeypatch):
    # GetMetricData bills per metric requested; MetricsPolicy caps the per-cycle
    # spend, not the daily total - the cadence is what caps the day.
    clock = _Clock()
    agent, policy = _metrics_agent(monkeypatch, clock, metrics_interval_seconds=3600)

    batch = agent._collect_batch()
    assert policy.remaining == 999            # one real read
    clock.advance(300)
    batch = agent._collect_batch()

    assert _CountingMetrics.collects == 1
    assert policy.remaining == 1000           # reset, and nothing spent it
    assert len(batch.metrics) == 1            # yet the series still reaches the detectors


def test_due_leaves_the_entry_alone_when_the_refresh_raises():
    clock = _Clock()
    due = Due(clock)

    assert due.get_or_reuse(("k",), 60, lambda: "good") == "good"
    clock.advance(61)
    try:
        due.get_or_reuse(("k",), 60, lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    except RuntimeError:
        pass
    assert due.last(("k",)) == "good"
