"""Rollup of a cycle's `Batch` into a summary, and its two renderings.

Pure unit tests — no CLI, no cloud. The money assertions are the point: totals
must stay exact `Decimal` and never mix currencies.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from clont.api.uplink import Batch
from clont.core.models import Cloud, CloudResource, Money, Period
from clont.events.models import Event, EventSeverity
from clont.finops.models import CostRecord, Recommendation
from clont.monitoring.models import HealthCheck, HealthStatus, MetricPoint
from clont.reporting.summary import SummaryFormat, render, render_json, render_text, summarize


def _resource(rid: str = "i-1") -> CloudResource:
    return CloudResource(cloud=Cloud.AWS, service="ec2", resource_id=rid, region="eu-west-1")


def _event(key: str, severity: EventSeverity, domain: str = "monitoring") -> Event:
    return Event(
        key=key,
        severity=severity,
        domain=domain,
        cloud=Cloud.AWS,
        title=f"title {key}",
        message=f"message {key}",
    )


def _cost(service: str, amount: str, currency: str = "USD") -> CostRecord:
    period = Period(start=datetime(2026, 1, 1, tzinfo=UTC).date(), end=datetime(2026, 1, 2, tzinfo=UTC).date())
    return CostRecord(
        cloud="aws",
        service=service,
        period=period,
        cost=Money(amount=Decimal(amount), currency=currency),
    )


def _batch() -> Batch:
    return Batch(
        metrics=[MetricPoint(name="CPUUtilization", value=1.0, unit="Percent",
                             timestamp=datetime.now(UTC))],
        costs=[_cost("ec2", "10.50"), _cost("s3", "2.25"), _cost("ec2", "0.25")],
        recommendations=[
            Recommendation(cloud="aws", service="ec2", resource=_resource(),
                           summary="idle", estimated_savings=Money(amount=Decimal("0.10"))),
            Recommendation(cloud="aws", service="ec2", resource=_resource("i-2"),
                           summary="idle", estimated_savings=Money(amount=Decimal("0.20"))),
        ],
        health=[
            HealthCheck(resource=_resource("i-1"), status=HealthStatus.OK, summary="fine"),
            HealthCheck(resource=_resource("i-2"), status=HealthStatus.CRITICAL, summary="down"),
        ],
        events=[
            _event("a", EventSeverity.INFO),
            _event("b", EventSeverity.CRITICAL),
            _event("c", EventSeverity.CRITICAL, domain="finops"),
        ],
    )


def test_summarize_counts_and_groupings():
    s = summarize(_batch(), accounts=["prod"], duration_seconds=1.2345)

    assert s.accounts == ["prod"]
    assert s.duration_seconds == round(1.2345, 3)          # rounded to ms
    assert (s.metrics, s.costs, s.recommendations, s.health_checks, s.events) == (1, 3, 2, 2, 3)
    assert s.events_by_severity == {"info": 1, "critical": 2}
    assert s.events_by_domain == {"monitoring": 2, "finops": 1}
    assert s.critical_events == 2
    assert s.health_by_status == {"ok": 1, "critical": 1}


def test_summarize_lists_only_unhealthy_checks():
    s = summarize(_batch())
    assert len(s.unhealthy) == 1
    assert "i-2" in s.unhealthy[0]
    assert "down" in s.unhealthy[0]


def test_savings_total_is_exact_decimal():
    s = summarize(_batch())
    total = s.estimated_savings[0]
    assert total.currency == "USD"
    # 0.10 + 0.20 as floats would be 0.30000000000000004.
    assert total.amount == Decimal("0.30")
    assert isinstance(total.amount, Decimal)


def test_savings_grouped_per_currency_never_summed_across():
    batch = Batch(recommendations=[
        Recommendation(cloud="aws", service="ec2", resource=_resource(), summary="idle",
                       estimated_savings=Money(amount=Decimal("1"), currency="USD")),
        Recommendation(cloud="aws", service="ec2", resource=_resource(), summary="idle",
                       estimated_savings=Money(amount=Decimal("2"), currency="EUR")),
    ])
    s = summarize(batch)
    assert [(t.currency, t.amount) for t in s.estimated_savings] == [
        ("EUR", Decimal("2")), ("USD", Decimal("1")),
    ]


def test_top_spend_aggregates_per_service_and_sorts_desc():
    s = summarize(_batch())
    assert [(x.service, x.amount) for x in s.top_spend] == [
        ("ec2", Decimal("10.75")),      # 10.50 + 0.25
        ("s3", Decimal("2.25")),
    ]


def test_errors_are_surfaced():
    s = summarize(Batch(errors=["finops X collect failed: boom"]))
    assert s.errors == ["finops X collect failed: boom"]
    assert "boom" in render_text(s)


def test_empty_batch_summarizes_without_crashing():
    s = summarize(Batch())
    assert s.events == 0
    assert s.events_by_severity == {}
    assert s.estimated_savings == []
    assert s.top_spend == []
    assert "none" in render_text(s)


def test_render_json_round_trips():
    data = json.loads(render_json(summarize(_batch(), accounts=["prod"])))
    assert data["accounts"] == ["prod"]
    assert data["events"] == 3
    assert data["events_by_severity"] == {"info": 1, "critical": 2}
    # Money stays a string so precision survives the JSON round trip.
    assert data["estimated_savings"] == [{"currency": "USD", "amount": "0.30"}]
    assert data["version"]


def test_render_text_has_headline_counts():
    text = render_text(summarize(_batch(), accounts=["prod"]))
    assert "accounts:   prod" in text
    assert "events:     3" in text
    assert "critical=2" in text
    assert "ec2: 10.75 USD" in text


def test_render_dispatches_on_format():
    s = summarize(Batch())
    assert render(s, SummaryFormat.JSON).lstrip().startswith("{")
    assert render(s, SummaryFormat.TEXT).startswith("clont scan summary")
