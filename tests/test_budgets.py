"""BudgetDetector: month-to-date + forecast vs operator-defined budgets."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from clont.core.config import BudgetRule
from clont.core.models import Money, Period
from clont.events.detectors import BudgetDetector
from clont.events.models import EventSeverity
from clont.finops.models import CostRecord


def _cost(service: str, amount: str, day: date, alias: str = "prod") -> CostRecord:
    return CostRecord(
        cloud="aws",
        service=service,
        period=Period(start=day, end=day),
        cost=Money(amount=Decimal(amount)),
        alias=alias,
    )


def _month(n_days: int, daily: str, service: str = "EC2", alias: str = "prod") -> list[CostRecord]:
    return [_cost(service, daily, date(2024, 1, 1) + timedelta(days=i), alias)
            for i in range(n_days)]


def _rule(limit: str, **kw) -> BudgetRule:
    return BudgetRule(monthly_limit=Decimal(limit), **kw)


def test_no_budgets_no_events():
    assert BudgetDetector([]).detect(_month(10, "10")) == []


def test_comfortably_under_budget_is_silent():
    # 10 days at $10 -> MTD 100, forecast ~310, budget 5000 (forecast < 80%).
    assert BudgetDetector([_rule("5000")]).detect(_month(10, "10")) == []


def test_actual_breach_is_critical():
    # MTD 100 already over a $50 budget.
    [event] = BudgetDetector([_rule("50")]).detect(_month(10, "10"))
    assert event.key == "finops:budget:prod:account"
    assert event.severity == EventSeverity.CRITICAL
    assert event.payload["limit"] == "50"


def test_projected_breach_is_warn():
    # MTD 100 < 200, but forecast ~310 >= 200 budget.
    [event] = BudgetDetector([_rule("200")]).detect(_month(10, "10"))
    assert event.severity == EventSeverity.WARN
    assert Decimal(event.payload["forecast"]) >= Decimal("200")


def test_approaching_budget_is_warn():
    # forecast ~310, budget 380 -> 80% of 380 = 304; 304 <= 310 < 380 -> approaching.
    [event] = BudgetDetector([_rule("380")], warn_pct=80).detect(_month(10, "10"))
    assert event.severity == EventSeverity.WARN
    assert "%" in event.title


def test_just_below_warn_threshold_is_silent():
    # forecast ~310, budget 400 -> 80% = 320; 310 < 320 -> nothing.
    assert BudgetDetector([_rule("400")], warn_pct=80).detect(_month(10, "10")) == []


def test_per_service_budget_only_counts_that_service():
    records = _month(10, "10", service="EC2") + _month(10, "100", service="RDS")
    # EC2 MTD 100 over a $50 EC2-only budget; RDS spend is ignored.
    [event] = BudgetDetector([_rule("50", service="EC2")]).detect(records)
    assert event.key == "finops:budget:prod:EC2"
    assert event.severity == EventSeverity.CRITICAL


def test_wildcard_account_fans_out():
    records = _month(10, "10", alias="prod") + _month(10, "10", alias="staging")
    events = BudgetDetector([_rule("50", account="*")]).detect(records)
    assert {e.key for e in events} == {
        "finops:budget:prod:account",
        "finops:budget:staging:account",
    }


def test_specific_account_does_not_match_others():
    records = _month(10, "10", alias="prod") + _month(10, "10", alias="staging")
    events = BudgetDetector([_rule("50", account="prod")]).detect(records)
    assert {e.key for e in events} == {"finops:budget:prod:account"}
