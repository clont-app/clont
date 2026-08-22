"""Commitment purchase advice derived from inventory, no Cost Explorer."""

from __future__ import annotations

from decimal import Decimal

import pytest

from clont.events.detectors import RecommendationDetector
from clont.finops.aws import inventory, pricing
from clont.finops.aws.commitments import CommitmentsCollector
from tests.test_finops_inventory import FakeProvider, instance, reserved


@pytest.fixture(autouse=True)
def _no_cache():
    inventory.clear_cache()
    yield
    inventory.clear_cache()


def _recs(**kw):
    return CommitmentsCollector(FakeProvider(**kw)).recommendations(None)


def test_savings_plan_recommendation_emitted():
    recs = _recs(instances={"us-east-1": [instance(f"i-{n}", "m5.4xlarge") for n in range(5)]})

    sp = [r for r in recs if r.service == "savings-plans"]
    assert len(sp) == 1
    [rec] = sp
    assert rec.kind == "savings-plan"
    assert rec.resource.resource_id == "COMPUTE_SP"
    assert rec.estimated_savings.amount > 0
    assert "Savings Plan" in rec.summary


def test_reserved_instance_recommendation_names_the_types():
    recs = _recs(instances={"us-east-1": [
        instance("i-1", "m5.4xlarge"), instance("i-2", "c5.9xlarge"),
    ]})

    [rec] = [r for r in recs if r.service == "reserved-instances"]
    assert rec.kind == "reserved-instance"
    assert rec.resource.resource_id == "ec2"
    assert "m5.4xlarge" in rec.summary and "c5.9xlarge" in rec.summary


def test_fully_covered_account_is_silent():
    recs = _recs(
        instances={"us-east-1": [instance("i-1")]},
        reserved={"us-east-1": [reserved(count=1)]},
    )
    assert recs == []


def test_nothing_running_is_silent():
    assert _recs(instances={"us-east-1": []}) == []


def test_savings_scale_with_the_discount_and_safety_factor():
    recs = _recs(instances={"us-east-1": [instance("i-1", "m5.4xlarge")]})
    hourly = pricing.instance_hourly("m5.4xlarge")
    expected = hourly * pricing.COMMIT_SAFETY * pricing.SP_DISCOUNT_PCT * pricing.HOURS_PER_MONTH

    [sp] = [r for r in recs if r.service == "savings-plans"]
    assert sp.estimated_savings.amount == expected.quantize(Decimal("0.01"))
    # the advice under-commits on purpose; never propose the full uncovered rate
    assert sp.estimated_savings.amount < hourly * pricing.HOURS_PER_MONTH


def test_summary_flags_the_snapshot_caveat():
    # Cost Explorer averaged 30 days; we don't. An operator comparing against the
    # console has to be told, or the mismatch reads as a bug.
    recs = _recs(instances={"us-east-1": [instance("i-1", "m5.4xlarge")]})
    assert recs
    assert all("snapshot" in r.summary for r in recs)


def test_spot_instances_do_not_drive_advice():
    assert _recs(instances={"us-east-1": [
        instance("i-spot", "m5.4xlarge", spot=True)
    ]}) == []


def test_one_bad_region_still_yields_advice():
    recs = _recs(
        instances={"us-east-1": [instance("i-1", "m5.4xlarge")]},
        broken={"eu-west-1"},
    )
    assert [r.service for r in recs] == ["savings-plans", "reserved-instances"]


def test_commitment_event_key_includes_kind():
    recs = _recs(instances={"us-east-1": [instance("i-1", "m5.4xlarge")]})
    keys = {e.key for e in RecommendationDetector().detect(recs)}
    assert "finops:rec:prod:aws:savings-plans:savings-plan:COMPUTE_SP" in keys
    assert "finops:rec:prod:aws:reserved-instances:reserved-instance:ec2" in keys


def test_collector_never_touches_cost_explorer():
    class _NoCE(FakeProvider):
        def client(self, service: str, region: str | None = None):
            assert service != "ce", "commitments must not call Cost Explorer"
            return super().client(service, region)

    recs = CommitmentsCollector(
        _NoCE(instances={"us-east-1": [instance("i-1", "m5.4xlarge")]})
    ).recommendations(None)
    assert recs
