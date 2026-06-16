"""Detectors fold the account alias into event keys + messages."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from clont.core.models import CloudResource, Cloud, Money, Period
from clont.events.detectors import (
    HealthDetector,
    RecommendationDetector,
    SpendDigestDetector,
    SpendSpikeDetector,
)
from clont.events.models import EventSeverity
from clont.finops.models import CostRecord, Recommendation
from clont.monitoring.models import HealthCheck, HealthStatus


def _cost(service: str, amount: str, day: date, alias: str = "prod") -> CostRecord:
    return CostRecord(
        cloud="aws",
        service=service,
        period=Period(start=day, end=day),
        cost=Money(amount=Decimal(amount)),
        alias=alias,
    )


def _rec(alias: str, kind: str = "rightsize", resource_id: str = "i-123") -> Recommendation:
    resource = CloudResource(
        cloud=Cloud.AWS, service="ec2", resource_id=resource_id, alias=alias
    )
    return Recommendation(
        cloud="aws",
        service="ec2",
        kind=kind,
        resource=resource,
        summary="idle instance",
        estimated_savings=Money(amount=Decimal("10")),
    )


def test_recommendation_key_and_title_include_alias():
    [event] = RecommendationDetector().detect([_rec("prod")])
    assert event.key == "finops:rec:prod:aws:ec2:rightsize:i-123"
    assert event.title.startswith("[prod]")


def test_same_resource_different_accounts_dont_collide():
    events = RecommendationDetector().detect([_rec("prod"), _rec("staging")])
    keys = {e.key for e in events}
    assert keys == {
        "finops:rec:prod:aws:ec2:rightsize:i-123",
        "finops:rec:staging:aws:ec2:rightsize:i-123",
    }


def test_missing_account_stays_wellformed():
    [event] = RecommendationDetector().detect([_rec(None)])
    assert event.key == "finops:rec:-:aws:ec2:rightsize:i-123"


def test_kind_disambiguates_same_resource():
    # An instance that is both rightsizable and idle must yield two distinct
    # keys, not collide into one.
    events = RecommendationDetector().detect(
        [_rec("prod", kind="rightsize"), _rec("prod", kind="idle")]
    )
    keys = {e.key for e in events}
    assert keys == {
        "finops:rec:prod:aws:ec2:rightsize:i-123",
        "finops:rec:prod:aws:ec2:idle:i-123",
    }


def test_spend_digest_one_info_event_per_alias_for_latest_day():
    records = [
        _cost("EC2", "10", date(2024, 1, 1), alias="prod"),
        _cost("EC2", "8", date(2024, 1, 2), alias="prod"),   # latest day for prod
        _cost("S3", "3", date(2024, 1, 2), alias="prod"),
        _cost("EC2", "5", date(2024, 1, 2), alias="staging"),
    ]
    events = SpendDigestDetector().detect(records)

    by_alias = {e.key: e for e in events}
    assert set(by_alias) == {"finops:spend:digest:prod", "finops:spend:digest:staging"}
    prod = by_alias["finops:spend:digest:prod"]
    assert prod.severity == EventSeverity.INFO
    assert prod.payload["total"] == "11"          # 8 + 3, only the latest day
    assert prod.payload["day"] == "2024-01-02"
    assert prod.title.startswith("[prod]")


def _spike_series(latest: str, alias: str = "prod") -> list[CostRecord]:
    # 3 baseline days at $10 + one latest day at `latest`.
    return [
        _cost("EC2", "10", date(2024, 1, 1), alias),
        _cost("EC2", "10", date(2024, 1, 2), alias),
        _cost("EC2", "10", date(2024, 1, 3), alias),
        _cost("EC2", latest, date(2024, 1, 4), alias),
    ]


def test_spend_spike_fires_above_threshold():
    [event] = SpendSpikeDetector(spike_pct=50, min_dollars=1).detect(_spike_series("20"))
    assert event.key == "finops:spend:spike:prod:aws:EC2"
    assert event.severity == EventSeverity.WARN
    assert event.title.startswith("[prod]")
    assert event.resource.resource_id == "EC2"


def test_spend_spike_silent_below_threshold():
    # 12 vs 10 baseline = +20%, under the 50% threshold.
    assert SpendSpikeDetector(spike_pct=50).detect(_spike_series("12")) == []


def test_spend_spike_silent_under_min_dollars():
    # Tiny absolute spend never trips, even if relatively huge.
    series = [
        _cost("EC2", "0.10", date(2024, 1, 1)),
        _cost("EC2", "0.10", date(2024, 1, 2)),
        _cost("EC2", "0.90", date(2024, 1, 3)),  # latest, big % jump but < $1
    ]
    assert SpendSpikeDetector(spike_pct=50, min_dollars=1).detect(series) == []


def test_spend_spike_silent_without_baseline():
    # Only one day -> no prior days -> can't spike.
    one_day = [_cost("EC2", "100", date(2024, 1, 1))]
    assert SpendSpikeDetector(spike_pct=50).detect(one_day) == []


def test_spend_spike_ignores_stale_service_not_on_anchor_day():
    # EC2 ran days 1-4 with a day-4 spike, then went quiet. RDS keeps spending
    # through day 5, so the account's anchor day is 5 — EC2's stale day-4 spike
    # must NOT fire, only services live on day 5 are evaluated.
    records = [
        _cost("EC2", "10", date(2024, 1, 1)),
        _cost("EC2", "10", date(2024, 1, 2)),
        _cost("EC2", "10", date(2024, 1, 3)),
        _cost("EC2", "50", date(2024, 1, 4)),   # local spike, but stale
        _cost("RDS", "10", date(2024, 1, 1)),
        _cost("RDS", "10", date(2024, 1, 4)),
        _cost("RDS", "10", date(2024, 1, 5)),   # anchor day, no spike
    ]
    assert SpendSpikeDetector(spike_pct=50).detect(records) == []


def test_spend_spike_uses_account_anchor_day():
    # Same account anchor (day 5); EC2 spikes ON the anchor day -> fires.
    records = [
        _cost("EC2", "10", date(2024, 1, 3)),
        _cost("EC2", "10", date(2024, 1, 4)),
        _cost("EC2", "50", date(2024, 1, 5)),   # anchor + spike
        _cost("RDS", "10", date(2024, 1, 5)),
    ]
    [event] = SpendSpikeDetector(spike_pct=50).detect(records)
    assert event.key == "finops:spend:spike:prod:aws:EC2"


_MONDAY = date(2024, 1, 29)  # a Monday; prior Mondays (Jan 8/15/22) are in a 28d window


def _weekly_series(anchor_amt: str, *, monday="100", other="20", days=28) -> list[CostRecord]:
    """28-day EC2 series with high Mondays; the anchor day uses `anchor_amt`."""
    recs = []
    for i in range(days):
        day = _MONDAY - timedelta(days=i)
        if day == _MONDAY:
            amt = anchor_amt
        else:
            amt = monday if day.weekday() == 0 else other
        recs.append(_cost("EC2", amt, day))
    return recs


def test_spend_spike_seasonal_normal_weekday_is_silent():
    # A normal Monday ($100) matches prior Mondays' median ($100), so it must NOT
    # fire — even though it's far above the all-days mean (which would false-fire).
    assert SpendSpikeDetector(spike_pct=50).detect(_weekly_series("100")) == []


def test_spend_spike_seasonal_abnormal_weekday_fires():
    # Double a normal Monday -> 200 vs same-weekday median 100 (+100%) -> fires.
    [event] = SpendSpikeDetector(spike_pct=50).detect(_weekly_series("200"))
    assert event.key == "finops:spend:spike:prod:aws:EC2"
    assert "same-weekday median" in event.message


def test_spend_spike_falls_back_to_flat_mean_on_short_window():
    # Too few same-weekday samples -> flat-mean baseline (original behaviour).
    [event] = SpendSpikeDetector(spike_pct=50, min_dollars=1).detect(_spike_series("20"))
    assert event.key == "finops:spend:spike:prod:aws:EC2"
    assert "avg" in event.message


def test_health_key_includes_alias():
    resource = CloudResource(
        cloud=Cloud.AWS, service="ec2", resource_id="i-9", alias="prod"
    )
    check = HealthCheck(resource=resource, status=HealthStatus.CRITICAL, summary="down")
    [event] = HealthDetector().detect([check])
    assert event.key == "monitoring:health:prod:aws:ec2:i-9"
    assert event.title.startswith("[prod]")
