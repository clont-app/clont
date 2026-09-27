"""Data transfer: usage-type classification, the report and its detector.

The properties worth pinning: classification never guesses (an unknown or
non-byte usage type is simply not transfer), the share is measured against the
whole bill and not just the network lines, and cloudfront egress is told apart
from direct egress by product name — the usage types are identical.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from clont.core.models import Cloud, Money, Period
from clont.events.detectors import DataTransferDetector
from clont.events.models import EventSeverity
from clont.finops.aws.usage_types import transfer_bucket
from clont.finops.models import CostRecord
from clont.finops.transfer import transfer_report

DAY = date(2024, 1, 1)


def _rec(
    amount: str,
    bucket: str | None = None,
    *,
    service: str = "Amazon Elastic Compute Cloud - Compute",
    alias: str = "prod",
    currency: str = "USD",
    day: date = DAY,
):
    return CostRecord(
        cloud=str(Cloud.AWS),
        service=service,
        period=Period(start=day, end=day),
        alias=alias,
        cost=Money(amount=Decimal(amount), currency=currency),
        dimensions={"transfer": bucket} if bucket else None,
    )


def test_usage_types_map_to_the_bucket_that_names_the_fix():
    assert transfer_bucket("USE1-DataTransfer-Regional-Bytes") == "cross-az"
    assert transfer_bucket("USE1-USW2-AWS-Out-Bytes") == "inter-region"
    assert transfer_bucket("USE1-USW2-AWS-In-Bytes") == "inter-region"
    assert transfer_bucket("DataTransfer-Out-Bytes") == "internet-out"
    assert transfer_bucket("USE1-DataTransfer-In-Bytes") == "internet-in"
    assert transfer_bucket("USE1-NatGateway-Bytes") == "nat"
    assert transfer_bucket("USE1-TransitGatewayDataProcessing-Bytes") == "transit-gateway"
    assert transfer_bucket("USE1-VpcEndpoint-Bytes") == "privatelink"


def test_non_byte_and_unknown_usage_types_are_not_transfer():
    # the hourly charges of the very same resources
    assert transfer_bucket("USE1-NatGateway-Hours") == ""
    assert transfer_bucket("BoxUsage:t3.micro") == ""
    assert transfer_bucket("PublicIPv4:InUseAddress") == ""
    assert transfer_bucket("") == ""
    assert transfer_bucket("SomethingNew-Bytes") == ""


def test_cloudfront_egress_is_told_from_direct_egress_by_product():
    # identical usage type, different product -> different fix
    assert transfer_bucket("US-DataTransfer-Out-Bytes") == "internet-out"
    assert transfer_bucket("US-DataTransfer-Out-Bytes", "Amazon CloudFront") == "cdn"


def test_share_is_measured_against_the_whole_bill():
    reports = transfer_report(
        [
            _rec("90"),  # compute, not transfer
            _rec("6", "nat"),
            _rec("4", "cross-az"),
        ]
    )
    assert len(reports) == 1
    report = reports[0]
    assert report.total == Decimal("100.00")
    assert report.transfer == Decimal("10.00")
    assert report.transfer_pct == Decimal("10.0")
    # biggest bucket first, share is of transfer spend
    assert [(ln.bucket, ln.amount, ln.share_pct) for ln in report.lines] == [
        ("nat", Decimal("6.00"), Decimal("60.0")),
        ("cross-az", Decimal("4.00"), Decimal("40.0")),
    ]


def test_top_talkers_per_bucket_are_named():
    report = transfer_report(
        [
            _rec("10", "internet-out", service="Amazon Simple Storage Service"),
            _rec("3", "internet-out", service="Amazon CloudFront"),
            _rec("1", "internet-out", service="Amazon Elastic Compute Cloud - Compute"),
        ],
        top_services=2,
    )[0]
    assert report.lines[0].services == (
        "Amazon Simple Storage Service",
        "Amazon CloudFront",
    )


def test_accounts_and_currencies_never_mix():
    reports = transfer_report(
        [
            _rec("10", "nat", alias="prod"),
            _rec("5", "nat", alias="dev"),
            _rec("7", "nat", alias="prod", currency="EUR"),
        ]
    )
    assert [(r.alias, r.currency, r.transfer) for r in reports] == [
        ("dev", "USD", Decimal("5.00")),
        ("prod", "EUR", Decimal("7.00")),
        ("prod", "USD", Decimal("10.00")),
    ]


def test_records_with_no_transfer_dimension_produce_no_report():
    assert transfer_report([_rec("100")]) == []


def test_detector_warns_over_the_share_and_names_the_buckets():
    events = DataTransferDetector(transfer_pct=15.0).detect(
        [_rec("70"), _rec("20", "nat"), _rec("10", "cross-az")]
    )
    assert len(events) == 1
    event = events[0]
    assert event.severity is EventSeverity.WARN
    assert event.key == "finops:transfer:prod"
    assert "30.0%" in event.title
    assert "nat" in event.message and "cross-az" in event.message
    assert event.payload["buckets"] == {"nat": "20.00", "cross-az": "10.00"}


def test_detector_stays_info_below_the_share():
    events = DataTransferDetector(transfer_pct=15.0).detect([_rec("95"), _rec("5", "nat")])
    assert events[0].severity is EventSeverity.INFO


def test_pennies_of_transfer_are_not_a_finding():
    # a toy account is 100% cross-az and still not worth an event
    events = DataTransferDetector(transfer_pct=15.0, min_dollars=1.0).detect(
        [_rec("0.20", "cross-az")]
    )
    assert events == []
