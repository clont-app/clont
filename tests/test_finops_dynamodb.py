"""DynamoDB capacity-mode advice, read off the CUR usage amounts.

The arithmetic that matters: one capacity unit covers 3600 requests an hour for
$0.00065 (writes, us-east-1), the same 3600 write request units on demand cost
$0.00225 — so provisioned wins above ~29% sustained utilization, and these tests
pin both sides of that line.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from clont.finops.aws.dynamodb import capacity_mode_recommendations, throughput_kind
from clont.finops.base import FinOpsTuning

ARN = "arn:aws:dynamodb:us-east-1:111122223333:table/orders"
WRU_RATE = Decimal("0.000000625")
START = datetime(2024, 1, 1)


def _hourly(
    hours: int, writes_per_hour: Decimal, reads_per_hour: Decimal = Decimal(0), *, step_hours: int = 1
) -> dict:
    """An on-demand table's throughput, one bucket per `step_hours`."""
    usage: dict = {}
    for i in range(hours):
        start = START + timedelta(hours=i * step_hours)
        if writes_per_hour:
            units = writes_per_hour * step_hours
            usage[(start, "", ARN, "write_requests")] = (units, units * WRU_RATE)
        if reads_per_hour:
            units = reads_per_hour * step_hours
            usage[(start, "", ARN, "read_requests")] = (units, units * Decimal("0.000000125"))
    return usage


def _advise(usage: dict, tuning: FinOpsTuning | None = None, currency: str = "USD"):
    return capacity_mode_recommendations(usage, {}, "prod", tuning, currency)


def test_steady_on_demand_traffic_is_advised_to_provision():
    # 1000 writes/sec for 100 hours: $225 billed, ~1429 WCU would cost ~$93
    [rec] = _advise(_hourly(100, Decimal(3_600_000)))
    assert rec.kind == "capacity-mode"
    assert rec.service == "dynamodb"
    assert rec.resource.resource_id == "orders"
    assert rec.resource.region == "us-east-1"
    assert rec.resource.alias == "prod"
    assert "1429 WCU" in rec.summary
    # saving/hour ~1.32 -> ~964 a month
    assert Decimal(900) < rec.estimated_savings.amount < Decimal(1000)


def test_a_trickle_of_traffic_stays_on_demand():
    # 60 writes/hour: 1 WCU covers it, and the saving is cents a month
    assert _advise(_hourly(100, Decimal(60))) == []


def test_provisioned_table_is_left_alone():
    # cur shows what was provisioned, not what was consumed -> no verdict
    usage = {
        (START + timedelta(hours=i), "", ARN, "wcu_hours"): (Decimal(500), Decimal("0.325"))
        for i in range(100)
    }
    assert _advise(usage) == []


def test_ia_table_class_is_skipped():
    usage = _hourly(100, Decimal(3_600_000))
    usage[(START, "", ARN, "other")] = (Decimal(1), Decimal(1))
    assert _advise(usage) == []


def test_short_window_is_not_enough_to_call_it_steady():
    assert _advise(_hourly(48, Decimal(3_600_000))) == []


def test_free_tier_only_traffic_has_nothing_to_save():
    usage = {(START + timedelta(hours=i), "", ARN, "write_requests"): (Decimal(10_000), Decimal(0))
             for i in range(100)}
    assert _advise(usage) == []


def test_daily_report_needs_double_the_margin():
    # same 1000 writes/sec, but one bucket per day: 58% saving clears 2x25%
    daily = _hourly(5, Decimal(3_600_000), step_hours=24)
    [rec] = _advise(daily)
    assert "daily CUR granularity" in rec.summary

    # at a 40% margin the doubled bar (80%) is above what steady traffic can show
    assert _advise(daily, FinOpsTuning(ddb_min_savings_pct=40.0)) == []


def test_hourly_report_is_modelled_hour_by_hour():
    [rec] = _advise(_hourly(100, Decimal(3_600_000)))
    assert "modelled hour by hour" in rec.summary


def test_idle_hours_still_cost_one_unit():
    # traffic in one hour of every four: the idle hours are not free under
    # provisioned billing, so the modelled cost carries a floor of 1 WCU+1 RCU
    usage = {}
    for i in range(100):
        start = START + timedelta(hours=i)
        units = Decimal(3_600_000) if i % 4 == 0 else Decimal(0)
        usage[(start, "", ARN, "write_requests")] = (units, units * WRU_RATE)
    [rec] = _advise(usage)
    assert "1429 WCU" not in rec.summary   # the median hour needs 1, not 1429


def test_min_savings_floor_silences_a_tiny_table():
    tuning = FinOpsTuning(ddb_min_savings_usd=10_000.0)
    assert _advise(_hourly(100, Decimal(3_600_000)), tuning) == []


def test_non_usd_bill_is_not_compared_against_a_usd_table():
    assert _advise(_hourly(100, Decimal(3_600_000)), currency="EUR") == []


def test_alias_comes_from_the_linked_account_map():
    usage = _hourly(100, Decimal(3_600_000))
    keyed = {(s, "999888777666", a, k): v for (s, _, a, k), v in usage.items()}
    [rec] = capacity_mode_recommendations(keyed, {"999888777666": "member"}, "payer")
    assert rec.resource.alias == "member"


def test_throughput_kind_tells_the_four_plain_skus_apart():
    assert throughput_kind("WriteRequestUnits") == "write_requests"
    assert throughput_kind("USW2-ReadCapacityUnit-Hrs") == "rcu_hours"
    assert throughput_kind("EUC1-WriteCapacityUnit-Hrs") == "wcu_hours"
    # not the plain skus: IA table class, global tables, vector writes
    assert throughput_kind("IA-WriteRequestUnits") == "other"
    assert throughput_kind("USW2-ReplWriteCapacityUnit-Hrs") == "other"
    # not throughput at all — same price in either mode
    assert throughput_kind("USW2-TimedStorage-ByteHrs") is None
    assert throughput_kind("USW2-TimedBackupStorage-ByteHrs") is None
