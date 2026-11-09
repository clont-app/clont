"""Agent spend detector: the envelope event and the cache finding."""

from datetime import date
from decimal import Decimal

from clont.agent.runner import Agent
from clont.core.models import Money, Period
from clont.events.detectors import AgentSpendDetector
from clont.events.models import EventSeverity
from clont.finops.base import FinOpsTuning
from clont.finops.models import CostRecord


def _rec(amount, bucket, agent=None, *, alias="prod", service="Amazon Bedrock"):
    return CostRecord(
        cloud="aws",
        service=service,
        period=Period(start=date(2024, 1, 1), end=date(2024, 1, 1)),
        cost=Money(amount=Decimal(amount), currency="USD"),
        alias=alias,
        dimensions={"agent": bucket} if bucket else None,
        tags={"agent": agent or ""},
    )


def _detector(**kw):
    return AgentSpendDetector(("agent",), **kw)


def _by_key(events):
    return {e.key: e for e in events}


def test_floor_heavy_agent_warns_with_the_envelope_biggest_first():
    events = _by_key(
        _detector(floor_pct=40.0).detect(
            [
                _rec("6.00", "floor", "rag", service="Amazon OpenSearch Service"),
                _rec("2.00", "tokens-in", "rag"),
                _rec("1.00", "cache-read", "rag"),
                _rec("1.00", "plumbing", "rag", service="AWS Lambda"),
                _rec("50.00", None, "rag", service="Amazon EC2"),
            ]
        )
    )
    event = events["finops:agent:prod:rag:USD"]
    assert event.severity is EventSeverity.WARN
    assert "60.0% fixed floor" in event.title
    msg = event.message
    assert msg.index("floor 6.00") < msg.index("tokens 3.00") < msg.index("plumbing 1.00")
    assert "180.00 USD/mo" in msg  # one day of floor restated per 30
    assert event.payload["floor_pct"] == "60.0"
    assert event.payload["unattributed"] == "false"


def test_token_heavy_agent_stays_info():
    events = _by_key(
        _detector(floor_pct=40.0).detect(
            [
                _rec("8.00", "tokens-in", "chat"),
                _rec("4.00", "cache-read", "chat"),
                _rec("1.00", "floor", "chat"),
            ]
        )
    )
    assert events["finops:agent:prod:chat:USD"].severity is EventSeverity.INFO
    assert "finops:agent-cache:prod:chat:USD" not in events


def test_agents_below_min_dollars_are_skipped():
    events = _detector(min_dollars=1.0).detect(
        [_rec("0.40", "floor", "toy"), _rec("0.30", "tokens-in", "toy")]
    )
    assert events == []


def test_unattributed_line_is_reported_and_labelled():
    events = _by_key(
        _detector().detect([_rec("5.00", "tokens-in"), _rec("2.00", "tokens-in", "rag")])
    )
    event = events["finops:agent:prod:(unattributed):USD"]
    assert "Unattributed agent spend: 5.00 USD" in event.title
    assert "inference profile" in event.message
    assert event.payload["unattributed"] == "true"
    assert "finops:agent:prod:rag:USD" in events


def test_uncached_input_tokens_warn_with_the_dollar_figure():
    events = _by_key(
        _detector(cache_min_share_pct=10.0).detect(
            [
                _rec("20.00", "tokens-in", "rag"),
                _rec("0.50", "cache-read", "rag"),
                _rec("5.00", "tokens-out", "rag"),
            ]
        )
    )
    event = events["finops:agent-cache:prod:rag:USD"]
    assert event.severity is EventSeverity.WARN
    # 0.50 of 20.50 input-side spend
    assert event.payload["cache_read_share"] == "2.4"
    assert event.payload["tokens_in"] == "20.00"
    assert "20.00 USD of uncached input tokens" in event.title


def test_cache_finding_needs_meaningful_input_spend():
    # the envelope still fires, a few cents of prompts does not
    events = _by_key(
        _detector(min_dollars=1.0).detect(
            [_rec("0.50", "tokens-in", "rag"), _rec("4.00", "floor", "rag")]
        )
    )
    assert "finops:agent:prod:rag:USD" in events
    assert "finops:agent-cache:prod:rag:USD" not in events


def test_cache_working_is_not_a_finding():
    events = _by_key(
        _detector(cache_min_share_pct=10.0).detect(
            [_rec("10.00", "tokens-in", "rag"), _rec("3.00", "cache-read", "rag")]
        )
    )
    assert "finops:agent-cache:prod:rag:USD" not in events


def test_off_without_tag_keys():
    records = [_rec("20.00", "tokens-in", "rag"), _rec("50.00", "floor", "rag")]
    assert AgentSpendDetector(()).detect(records) == []


def _agent_detector(agent):
    return next(d for d in agent._finops_cost_detectors if isinstance(d, AgentSpendDetector))


def test_runner_wires_the_tuning_and_thresholds():
    agent = Agent(
        [],
        [],
        finops_tuning=FinOpsTuning(agent_tag_keys=("agent",)),
        agent_floor_pct=25.0,
        agent_min_dollars=3.0,
        agent_cache_min_share_pct=5.0,
    )
    detector = _agent_detector(agent)
    assert detector._keys == ("agent",)
    assert detector._limit == Decimal("25.0")
    assert detector._min == Decimal("3.0")
    assert detector._cache_min == Decimal("5.0")


def test_runner_default_is_off():
    detector = _agent_detector(Agent([], []))
    assert detector.detect([_rec("20.00", "tokens-in", "rag")]) == []
