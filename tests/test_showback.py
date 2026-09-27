"""Showback grouping and its detector.

The properties worth pinning: an untagged line is its own reported bucket rather
than a silent omission, records from a collector that knows no tags stay out of
that bucket, and currencies never mix.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from clont.core.models import Cloud, Money, Period
from clont.events.detectors import ShowbackDetector
from clont.events.models import EventSeverity
from clont.finops.models import CostRecord
from clont.finops.showback import UNATTRIBUTED, showback

DAY = date(2024, 1, 1)


def _rec(amount: str, tags: dict[str, str] | None, *, alias="prod", currency="USD", day=DAY):
    return CostRecord(
        cloud=str(Cloud.AWS),
        service="ec2",
        period=Period(start=day, end=day),
        alias=alias,
        cost=Money(amount=Decimal(amount), currency=currency),
        tags=tags,
    )


def test_spend_groups_by_tag_value_with_shares():
    reports = showback(
        [
            _rec("60", {"Owner": "team-a"}),
            _rec("30", {"Owner": "team-b"}),
            _rec("10", {"Owner": "team-a"}),
        ],
        ("Owner",),
    )

    assert len(reports) == 1
    report = reports[0]
    assert report.key == "Owner"
    assert report.total == Decimal("100.00")
    assert [(ln.value, ln.amount, ln.share_pct) for ln in report.lines] == [
        ("team-a", Decimal("70.00"), Decimal("70.0")),
        ("team-b", Decimal("30.00"), Decimal("30.0")),
    ]
    assert report.unattributed == Decimal("0.00")


def test_missing_and_blank_values_are_one_unattributed_line():
    reports = showback(
        [_rec("75", {"Owner": "team-a"}), _rec("20", {"Owner": "  "}), _rec("5", {})],
        ("Owner",),
    )

    report = reports[0]
    assert report.unattributed == Decimal("25.00")
    assert report.unattributed_pct == Decimal("25.0")
    # reported as a line too, so a rendering that only walks lines still shows it
    assert (UNATTRIBUTED, Decimal("25.00")) in [(ln.value, ln.amount) for ln in report.lines]


def test_records_without_tags_are_not_unattributed():
    # a synthetic run-rate record (public ipv4) knows no tags; counting it as
    # untagged would make coverage look worse than it is
    reports = showback([_rec("90", {"Owner": "team-a"}), _rec("10", None)], ("Owner",))

    report = reports[0]
    assert report.total == Decimal("90.00")
    assert report.unattributed == Decimal("0.00")


def test_each_key_account_and_currency_gets_its_own_report():
    reports = showback(
        [
            _rec("10", {"Owner": "a", "Environment": "prod"}),
            _rec("20", {"Owner": "b", "Environment": "prod"}, alias="dev"),
            _rec("30", {"Owner": "a", "Environment": "prod"}, currency="EUR"),
        ],
        ("Owner", "Environment"),
    )

    assert {(r.alias, r.key, r.currency, r.total) for r in reports} == {
        ("dev", "Owner", "USD", Decimal("20.00")),
        ("dev", "Environment", "USD", Decimal("20.00")),
        ("prod", "Owner", "USD", Decimal("10.00")),
        ("prod", "Environment", "USD", Decimal("10.00")),
        ("prod", "Owner", "EUR", Decimal("30.00")),
        ("prod", "Environment", "EUR", Decimal("30.00")),
    }


def test_window_spans_the_records():
    reports = showback(
        [
            _rec("5", {"Owner": "a"}, day=date(2024, 1, 3)),
            _rec("5", {"Owner": "a"}, day=date(2024, 1, 1)),
        ],
        ("Owner",),
    )

    assert (reports[0].start, reports[0].end) == (date(2024, 1, 1), date(2024, 1, 3))


def test_no_keys_configured_is_a_no_op():
    assert showback([_rec("10", {"Owner": "a"})], ()) == []
    assert ShowbackDetector().detect([_rec("10", {"Owner": "a"})]) == []


def test_detector_warns_once_unattributed_crosses_the_threshold():
    records = [_rec("70", {"Owner": "team-a"}), _rec("30", {"Owner": ""})]

    (event,) = ShowbackDetector(("Owner",), unattributed_pct=20.0).detect(records)
    assert event.severity is EventSeverity.WARN
    assert event.key == "finops:showback:prod:Owner"
    assert "30.0% unattributed" in event.title
    assert "team-a 70.00" in event.message
    assert event.payload["unattributed"] == "30.00"
    assert event.payload["values"][UNATTRIBUTED] == "30.00"

    (quiet,) = ShowbackDetector(("Owner",), unattributed_pct=40.0).detect(records)
    assert quiet.severity is EventSeverity.INFO


def test_detector_skips_a_window_with_no_spend():
    # pure credits net to zero; there is no share to report
    assert ShowbackDetector(("Owner",)).detect([_rec("0", {"Owner": "a"})]) == []
