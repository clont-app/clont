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
from clont.reporting.summary import (
    SummaryFormat,
    render,
    render_json,
    render_report,
    render_text,
    summarize,
)


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
    assert "CLOUD WASTE REPORT" in render(s, SummaryFormat.REPORT)


def _rec(kind: str, rid: str, amount: str, service: str = "ec2") -> Recommendation:
    return Recommendation(
        cloud="aws", service=service, resource=_resource(rid), summary=f"{kind} finding",
        estimated_savings=Money(amount=Decimal(amount)), kind=kind,
    )


def test_findings_group_by_kind_and_sort_by_money():
    batch = Batch(recommendations=[
        _rec("waste", "eip-1", "3.60"),
        _rec("idle_nat", "nat-1", "97.20"),
        _rec("idle_nat", "nat-2", "97.20"),
    ])
    findings = summarize(batch).findings

    assert [(f.kind, f.count, f.amount) for f in findings] == [
        ("idle_nat", 2, Decimal("194.40")),        # biggest first
        ("waste", 1, Decimal("3.60")),
    ]


def test_findings_never_group_across_currencies():
    batch = Batch(recommendations=[
        Recommendation(cloud="aws", service="ec2", resource=_resource(), summary="idle",
                       estimated_savings=Money(amount=Decimal("1"), currency="USD"), kind="idle"),
        Recommendation(cloud="aws", service="ec2", resource=_resource(), summary="idle",
                       estimated_savings=Money(amount=Decimal("2"), currency="EUR"), kind="idle"),
    ])
    findings = summarize(batch).findings

    assert len(findings) == 2
    assert {(f.currency, f.amount) for f in findings} == {
        ("USD", Decimal("1")), ("EUR", Decimal("2")),
    }


def test_finding_examples_are_capped_at_the_biggest():
    batch = Batch(recommendations=[_rec("idle_nat", f"nat-{i}", str(i)) for i in range(1, 8)])
    finding = summarize(batch).findings[0]

    assert finding.count == 7
    assert [e.resource_id for e in finding.examples] == ["nat-7", "nat-6", "nat-5"]


def test_report_leads_with_monthly_and_annual_money():
    batch = Batch(recommendations=[_rec("idle_nat", "nat-1", "1229.75")])
    report = render_report(summarize(batch, accounts=["prod"]))

    assert "you're wasting" in report
    assert "$1,229.75 / month" in report                 # thousands separator, 2dp
    assert "$14,757.00 / year" in report                 # x12


def test_report_names_the_resources():
    report = render_report(summarize(Batch(recommendations=[_rec("idle_nat", "nat-0a1b", "97.20")])))

    assert "idle_nat (ec2)" in report
    assert "nat-0a1b" in report
    assert "eu-west-1" in report


def test_report_truncates_long_finding_lists():
    batch = Batch(recommendations=[_rec("idle_nat", f"nat-{i}", "10") for i in range(9)])

    assert "... and 6 more" in render_report(summarize(batch))   # 9 total, 3 shown


def test_report_flags_partial_scans_next_to_the_number():
    batch = Batch(recommendations=[_rec("idle_nat", "nat-1", "10")],
                  errors=["ce:GetCostAndUsage AccessDenied"])

    headline, _, rest = render_report(summarize(batch)).partition("WHERE THE MONEY GOES")

    assert "floor, not a total" in headline              # warning sits above the detail
    assert "AccessDenied" in rest


def test_report_does_not_claim_all_clear_when_the_scan_failed():
    report = render_report(summarize(Batch(errors=["boom"])))

    assert "did not complete" in report
    assert "no recoverable waste" not in report


def test_report_handles_an_empty_batch():
    report = render_report(summarize(Batch(), accounts=["prod"]))

    assert "no recoverable waste found" in report
    assert "WHERE THE MONEY GOES" not in report          # no empty section headers


def test_report_says_so_when_nothing_was_configured():
    report = render_report(summarize(Batch()))

    assert "no accounts configured" in report
    assert "no recoverable waste" not in report
