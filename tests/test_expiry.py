"""Commitment expiry calendar: tiers, lapse cost, and what stays quiet."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from clont.events.detectors import RecommendationDetector
from clont.finops.aws import inventory
from clont.finops.aws.expiry import CommitmentExpiryCollector
from tests.test_finops_inventory import FakeProvider, plan, reserved


@pytest.fixture(autouse=True)
def _no_cache():
    inventory.clear_cache()
    yield
    inventory.clear_cache()


def _in_days(days: float) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _ri(days: float | None, itype: str = "m5.large", count: int = 1, **kw) -> dict:
    raw = reserved(itype, count, **kw)
    if days is not None:
        raw["End"] = _in_days(days)
    return raw


def _sp(days: float | None, commitment: str = "1.00", **kw) -> dict:
    raw = plan(commitment, **kw)
    if days is not None:
        raw["end"] = _in_days(days)
    return raw


def _recs(**kw):
    inventory.clear_cache()  # every call is its own account snapshot
    return CommitmentExpiryCollector(FakeProvider(**kw)).recommendations(None)


def test_ri_expiring_soon_reports_its_tier():
    [rec] = _recs(reserved={"us-east-1": [_ri(5.5)]})
    assert rec.kind == "commitment-expiry-7d"
    assert rec.service == "reserved-instances"
    assert "expires in 5 days" in rec.summary
    assert "m5.large" in rec.summary
    assert rec.estimated_savings.amount > 0


@pytest.mark.parametrize(
    ("days", "kind"),
    [
        (2, "commitment-expiry-7d"),
        (7, "commitment-expiry-7d"),
        (8, "commitment-expiry-30d"),
        (30, "commitment-expiry-30d"),
        (31, "commitment-expiry-60d"),
        (59, "commitment-expiry-60d"),
    ],
)
def test_tier_is_the_tightest_threshold_crossed(days, kind):
    # each tier is its own kind (so its own event key) — otherwise a channel
    # that sends once would alert at 60 days and stay silent at 7
    [rec] = _recs(reserved={"us-east-1": [_ri(days + 0.5)]})
    assert rec.kind == kind


def test_commitment_further_out_is_silent():
    assert _recs(reserved={"us-east-1": [_ri(120)]}) == []


def test_no_end_date_is_silent():
    assert _recs(reserved={"us-east-1": [_ri(None)]}, plans=[_sp(None)]) == []


def test_already_lapsed_says_so_instead_of_expires_today():
    # aws keeps a lapsed ri active for a while; "expires today" on a date three
    # days gone reads as "still time" on the most urgent case there is
    [rec] = _recs(reserved={"us-east-1": [_ri(-3)]})
    assert rec.kind == "commitment-expiry-7d"
    assert "expired 3 days ago" in rec.summary
    assert "expires" not in rec.summary


def test_lapsed_yesterday_is_singular():
    [rec] = _recs(reserved={"us-east-1": [_ri(-1.5)]})
    assert "expired 1 day ago" in rec.summary


def test_naive_end_date_does_not_sink_the_pass():
    # a tz-less End compared to an aware now raises TypeError and kills the run
    raw = _ri(3)
    raw["End"] = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=3.5)
    [rec] = _recs(reserved={"us-east-1": [raw]})
    assert "expires in 3 days" in rec.summary


def test_retired_ri_never_reaches_the_calendar():
    # the inventory join drops non-active reservations
    assert _recs(reserved={"us-east-1": [_ri(3, state="retired")]}) == []


def test_ri_lapse_cost_scales_with_count():
    [one] = _recs(reserved={"us-east-1": [_ri(3, count=1)]})
    [four] = _recs(reserved={"us-east-1": [_ri(3, count=4)]})
    assert four.estimated_savings.amount == one.estimated_savings.amount * 4
    assert "4x m5.large" in four.summary


def test_az_scoped_ri_names_the_az():
    [rec] = _recs(reserved={"us-east-1": [
        _ri(3, scope="Availability Zone", az="us-east-1a")
    ]})
    assert "us-east-1/us-east-1a" in rec.summary


def test_savings_plan_expiry_quotes_the_replacement_commitment():
    [rec] = _recs(plans=[_sp(20, "2.50")])
    assert rec.kind == "commitment-expiry-30d"
    assert rec.service == "savings-plans"
    assert "2.50 USD/hr" in rec.summary
    assert "buy a replacement" in rec.summary
    # committed $/hr is already discounted, so the uplift is d/(1-d) of it
    assert rec.estimated_savings.amount == pytest.approx(456.25)


def test_non_compute_plan_still_watched():
    # sagemaker/database plans don't cover ec2 (utilization ignores them) but
    # they lapse the same way
    [rec] = _recs(plans=[_sp(10, "1.00", plan_type="SageMaker")])
    assert rec.resource.service == "savings-plans"
    assert "SageMaker Savings Plan" in rec.summary
    # the figure is the compute discount; don't pass it off as sagemaker's
    assert "compute discount" in rec.summary


def test_plan_in_another_currency_keeps_that_currency():
    [rec] = _recs(plans=[_sp(10, "2.00", currency="EUR")])
    assert "2.00 EUR/hr" in rec.summary
    assert rec.estimated_savings.currency == "EUR"


def test_inactive_plan_is_not_a_commitment():
    assert _recs(plans=[_sp(10, state="queued")]) == []


def test_each_tier_is_its_own_event_key():
    ri = _recs(reserved={"us-east-1": [_ri(3)]})
    later = _recs(reserved={"us-east-1": [_ri(45)]})
    keys = {e.key for e in RecommendationDetector().detect(ri + later)}
    assert len(keys) == 2
    assert any(k.endswith("ri-m5.large-Region") for k in keys)
