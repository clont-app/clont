from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from clont.core.models import Money, Period
from clont.finops.aws import idle_agent
from clont.finops.aws.idle_agent import KIND, IdleAgentCollector, idle_agent_recommendations
from clont.finops.aws.offhours import _KIND as OFFHOURS_KIND
from clont.finops.base import FinOpsTuning
from clont.finops.models import CostRecord

START = date(2026, 10, 1)
KEYS = ("agent",)


def _rec(day, bucket, agent="rag", amount="10", *, alias="prod", **tags):
    d = START + timedelta(days=day)
    return CostRecord(
        cloud="aws",
        service="Amazon OpenSearch Service" if bucket == "floor" else "Amazon Bedrock",
        period=Period(start=d, end=d),
        cost=Money(amount=Decimal(amount)),
        alias=alias,
        dimensions={"agent": bucket},
        tags={"agent": agent, **tags},
    )


def _floor(days=20, agent="rag", **tags):
    return [_rec(d, "floor", agent, **tags) for d in range(days)]


def test_idle_floor_saves_its_own_run_rate():
    # tokens only before the window: days 0-5, window is days 6-19
    records = _floor() + [_rec(d, "tokens-in", amount="3") for d in range(6)]
    (rec,) = idle_agent_recommendations(records, KEYS, 14)
    assert rec.kind == KIND
    assert rec.service == "agent"
    assert rec.resource.resource_id == "rag"
    assert rec.resource.alias == "prod"
    # 14 days x 10 over 14 days, x 30
    assert rec.estimated_savings == Money(amount=Decimal("300.00"), currency="USD")
    assert rec.approximate is False
    assert "300.00 USD/mo" in rec.summary


def test_recent_tokens_mean_not_idle():
    records = _floor() + [_rec(18, "cache-read", amount="0.5")]
    assert idle_agent_recommendations(records, KEYS, 14) == []


def test_tokens_of_another_agent_do_not_count():
    records = _floor() + [_rec(18, "tokens-out", agent="other")]
    (rec,) = idle_agent_recommendations(records, KEYS, 14)
    assert rec.resource.resource_id == "rag"


def test_nonprod_agent_is_an_off_hours_candidate():
    records = _floor(env="Dev")
    (rec,) = idle_agent_recommendations(records, KEYS, 14, {"env": ("dev", "staging")})
    assert rec.kind == OFFHOURS_KIND
    assert "env=Dev" in rec.summary
    assert rec.approximate is True
    # 300/mo floor, 108 of 168 hours off
    assert rec.estimated_savings.amount == Decimal("192.86")


def test_unattributed_floor_is_never_called_idle():
    assert idle_agent_recommendations(_floor(agent=""), KEYS, 14) == []


def test_short_history_emits_nothing():
    assert idle_agent_recommendations(_floor(days=13), KEYS, 14) == []


def test_floor_that_started_inside_the_window_is_not_idle():
    # a new stack, days 10-19: not billed on the window's first day
    records = [_rec(d, "floor") for d in range(10, 20)] + [_rec(0, "plumbing")]
    assert idle_agent_recommendations(records, KEYS, 14) == []


def test_floor_that_stopped_is_not_idle():
    records = [_rec(d, "floor") for d in range(15)] + [_rec(19, "plumbing")]
    assert idle_agent_recommendations(records, KEYS, 14) == []


def test_off_without_tag_keys():
    assert idle_agent_recommendations(_floor(), (), 14) == []


class _Provider:
    alias = "prod"
    cur = object()

    def client(self, *a, **kw):  # pragma: no cover - must never be reached
        raise AssertionError("no api call when the feature is off")


def test_collector_is_off_without_tag_keys():
    collector = IdleAgentCollector(_Provider(), FinOpsTuning())
    assert collector.recommendations(Period(start=START, end=START)) == []


def test_collector_reads_cur_with_nonprod_keys_in_the_split(monkeypatch):
    seen = {}

    def fake_collect(self, period):
        seen["tags"] = self._tags
        seen["period"] = period
        return _floor(env="dev")

    monkeypatch.setattr(idle_agent.CURCostCollector, "collect", fake_collect)
    tuning = FinOpsTuning(
        agent_tag_keys=KEYS, agent_idle_days=14, nonprod_tags={"env": ("dev",)},
        required_tags=("team",),
    )
    (rec,) = IdleAgentCollector(_Provider(), tuning).recommendations(
        Period(start=START, end=START)
    )
    assert rec.kind == OFFHOURS_KIND
    assert seen["tags"] == ("team", "env", "agent")
    assert (seen["period"].end - seen["period"].start).days >= 14
