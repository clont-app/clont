"""The guard that keeps the billed `GetMetricData` out of a default cycle.

Sibling of `test_finops_no_cost_explorer`. CloudWatch bills per *metric
requested*, so it is the one meter that scales with the customer's fleet — and
the idle detectors asked for one metric per resource per cycle. Compute Optimizer
now answers the same question for free, so a default cycle must not touch
`cloudwatch` at all.

Same recording-provider trick as the CE guard: `for_each_region` swallows
per-region exceptions, so a fake that *raised* would be caught by the very
isolation this is meant to see through and the test would pass for the wrong
reason.
"""

from __future__ import annotations

from datetime import date

import pytest

from clont.core import registry
from clont.core.models import Cloud, Period
from clont.finops.aws import inventory
from clont.finops.aws.idle import IdleCollector
from clont.finops.base import FinOpsTuning
from tests.test_finops_no_cost_explorer import _Recording
from tests.test_finops_inventory import instance


def _period() -> Period:
    return Period(date(2026, 1, 1), date(2026, 1, 31))


@pytest.fixture(autouse=True)
def _no_cache():
    inventory.clear_cache()
    yield
    inventory.clear_cache()


@pytest.mark.parametrize(
    "cls",
    list(registry.collectors_for("finops", Cloud.AWS)),
    ids=lambda c: c.service,
)
def test_no_finops_collector_calls_cloudwatch(cls):
    provider = _Recording(instances={"us-east-1": [instance("i-1")]})
    collector = cls(provider)
    try:
        collector.recommendations(_period())
        collector.collect(_period())
    except NotImplementedError:
        pytest.skip(f"{cls.service} is still a stub")
    assert "cloudwatch" not in provider.asked


def test_the_guard_would_notice_a_cloudwatch_call():
    # Proof a recording provider sees through for_each_region's isolation: the
    # same collector, opted in, does reach cloudwatch.
    from tests.test_idle import _FakeCW, _FakeEC2, _FakeProvider

    class _Rec(_FakeProvider):
        asked: list[str] = []

        def client(self, service: str, region: str | None = None):
            self.asked.append(service)
            return super().client(service, region)

    provider = _Rec(_FakeEC2(["i-1"]), _FakeCW({"i-1": {"CPUUtilization": [1.0]}}))
    IdleCollector(provider, FinOpsTuning(allow_cloudwatch_metrics=True)).recommendations(
        _period()
    )
    assert "cloudwatch" in provider.asked


def test_idle_advice_still_arrives_without_cloudwatch():
    """The point of the change: zero paid calls, and idle recs are still produced."""
    from tests.test_co_idle import _idle
    from tests.test_compute_optimizer import _FakeCO
    from clont.finops.aws.compute_optimizer import ComputeOptimizerCollector

    class _Provider(_Recording):
        def client(self, service: str, region: str | None = None):
            self.asked.append(service)
            assert service != "cloudwatch"
            return _FakeCO(idle={"idleRecommendations": [_idle("i-1", "EC2Instance")]})

    provider = _Provider()
    [rec] = ComputeOptimizerCollector(provider).recommendations(_period())
    assert rec.kind == "idle"
    assert rec.resource.resource_id == "i-1"
    assert rec.estimated_savings.amount > 0  # cloudwatch could never say this
