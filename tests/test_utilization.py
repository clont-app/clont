"""Commitment utilization & coverage, derived from inventory (no Cost Explorer)."""

from __future__ import annotations

import pytest

from clont.finops.aws import inventory
from clont.finops.aws.utilization import CommitmentUtilizationCollector
from clont.finops.base import FinOpsTuning
from tests.test_finops_inventory import FakeProvider, instance, plan, reserved


@pytest.fixture(autouse=True)
def _no_cache():
    inventory.clear_cache()
    yield
    inventory.clear_cache()


def _collect(*, tuning=None, **kw):
    coll = CommitmentUtilizationCollector(FakeProvider(**kw), tuning)
    return coll.recommendations(None)


def _kinds(recs):
    return sorted(r.kind for r in recs)


def test_low_sp_utilization_flagged():
    # $5/hr committed against a single small instance -> almost nothing consumed.
    recs = _collect(
        instances={"us-east-1": [instance("i-1", "m5.large")]},
        plans=[plan("5.00")],
    )
    [rec] = [r for r in recs if r.kind == "low-sp-utilization"]
    assert rec.service == "savings-plans"
    assert rec.estimated_savings.amount > 0
    assert "utilized" in rec.summary


def test_high_sp_utilization_silent():
    # Far more eligible usage than committed -> the plan is fully consumed.
    recs = _collect(
        instances={"us-east-1": [instance(f"i-{n}", "m5.24xlarge") for n in range(4)]},
        plans=[plan("1.00")],
    )
    assert [r for r in recs if r.kind == "low-sp-utilization"] == []


def test_no_commitment_held_is_silent_about_utilization():
    recs = _collect(instances={"us-east-1": [instance("i-1")]})
    assert "low-sp-utilization" not in _kinds(recs)
    assert "low-ri-utilization" not in _kinds(recs)


def test_low_sp_coverage_flagged():
    recs = _collect(instances={"us-east-1": [instance("i-1", "m5.4xlarge")]})
    [rec] = [r for r in recs if r.kind == "low-sp-coverage"]
    assert "0%" in rec.summary
    assert "could be committed" in rec.summary


def test_coverage_silent_without_uncovered_usage():
    recs = _collect(
        instances={"us-east-1": [instance("i-1")]},
        reserved={"us-east-1": [reserved(count=1)]},
    )
    assert [r for r in recs if r.kind in {"low-sp-coverage", "low-ri-coverage"}] == []


def test_low_ri_utilization_flagged():
    recs = _collect(
        instances={"us-east-1": [instance("i-1")]},
        reserved={"us-east-1": [reserved(count=4)]},
    )
    [rec] = [r for r in recs if r.kind == "low-ri-utilization"]
    assert rec.service == "reserved-instances"
    assert "25%" in rec.summary
    assert "reserved hours unused" in rec.summary


def test_low_ri_coverage_flagged():
    # Two running, one reserved -> 50% covered, under the 70% default gate.
    recs = _collect(
        instances={"us-east-1": [instance("i-1"), instance("i-2")]},
        reserved={"us-east-1": [reserved(count=1)]},
    )
    [rec] = [r for r in recs if r.kind == "low-ri-coverage"]
    assert "50%" in rec.summary


def test_thresholds_are_configurable():
    kw = {
        "instances": {"us-east-1": [instance("i-1")]},
        "reserved": {"us-east-1": [reserved(count=2)]},  # 50% utilized
    }
    assert [r for r in _collect(**kw) if r.kind == "low-ri-utilization"]
    lenient = FinOpsTuning(ri_sp_min_utilization=40.0, ri_sp_min_coverage=40.0)
    assert [r for r in _collect(tuning=lenient, **kw) if r.kind == "low-ri-utilization"] == []


def test_summaries_flag_the_snapshot_caveat():
    recs = _collect(
        instances={"us-east-1": [instance("i-1")]},
        reserved={"us-east-1": [reserved(count=4)]},
        plans=[plan("5.00")],
    )
    assert recs
    assert all("snapshot" in r.summary for r in recs)


def test_one_bad_region_does_not_drop_the_rest():
    recs = _collect(
        instances={"us-east-1": [instance("i-1")]},
        reserved={"us-east-1": [reserved(count=4)]},
        broken={"eu-west-1"},
    )
    assert "low-ri-utilization" in _kinds(recs)


def test_missing_savingsplans_grant_still_yields_ri_findings():
    from botocore.exceptions import ClientError

    class _Denied(FakeProvider):
        def client(self, service: str, region: str | None = None):
            if service == "savingsplans":
                raise ClientError(
                    {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                    "DescribeSavingsPlans",
                )
            return super().client(service, region)

    coll = CommitmentUtilizationCollector(_Denied(
        instances={"us-east-1": [instance("i-1")]},
        reserved={"us-east-1": [reserved(count=4)]},
    ))
    assert "low-ri-utilization" in _kinds(coll.recommendations(None))


def test_collector_never_touches_cost_explorer():
    class _NoCE(FakeProvider):
        def client(self, service: str, region: str | None = None):
            assert service != "ce", "utilization must not call Cost Explorer"
            return super().client(service, region)

    coll = CommitmentUtilizationCollector(_NoCE(
        instances={"us-east-1": [instance("i-1", "m5.4xlarge")]}
    ))
    assert coll.recommendations(None)
