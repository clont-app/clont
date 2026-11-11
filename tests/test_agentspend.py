from datetime import date
from decimal import Decimal

from clont.core.models import Money, Period
from clont.finops.agentspend import UNATTRIBUTED, agent_report
from clont.finops.models import CostRecord


def _rec(amount, bucket=None, agent=None, *, day=1, alias="prod", service="Amazon Bedrock"):
    return CostRecord(
        cloud="aws",
        service=service,
        period=Period(start=date(2024, 1, day), end=date(2024, 1, day)),
        cost=Money(amount=Decimal(amount), currency="USD"),
        alias=alias,
        dimensions={"agent": bucket} if bucket else None,
        tags={"agent": agent or ""},
    )


def _records():
    return [
        _rec("3.00", "tokens-in", "rag"),
        _rec("1.50", "tokens-out", "rag"),
        _rec("1.00", "cache-read", "rag"),
        _rec("5.76", "floor", "rag", service="Amazon OpenSearch Service"),
        _rec("5.76", "floor", "rag", day=2, service="Amazon OpenSearch Service"),
        _rec("0.40", "plumbing", "rag", service="AWS Lambda"),
        # on-demand invoke, no profile and no role tag
        _rec("0.25", "tokens-in"),
        # not agent spend at all
        _rec("9.00", None, "rag", service="Amazon EC2"),
    ]


def test_no_keys_means_no_report():
    assert agent_report(_records(), ()) == []


def test_envelope_per_agent():
    rag, rest = agent_report(_records(), ("agent",))

    assert rag.agent == "rag"
    assert rag.total == Decimal("17.42")
    assert rag.tokens == Decimal("5.50")
    assert rag.floor == Decimal("11.52")
    assert rag.plumbing == Decimal("0.40")
    assert rag.floor_pct == Decimal("66.1")
    assert [line.bucket for line in rag.lines] == [
        "floor", "tokens-in", "tokens-out", "cache-read", "plumbing",
    ]
    assert (rag.start, rag.end) == (date(2024, 1, 1), date(2024, 1, 2))
    # 11.52 over two days, restated per 30
    assert rag.monthly_floor_run_rate == Decimal("172.80")
    assert rag.cache_read_share == Decimal("25.0")

    assert rest.agent == UNATTRIBUTED
    assert rest.total == Decimal("0.25")
    assert rest.cache_read_share == Decimal("0.0")


def test_untagged_spend_is_never_spread_over_agents():
    reports = agent_report(_records(), ("agent",))

    assert sum(r.total for r in reports) == Decimal("17.67")
    assert reports[-1].agent == UNATTRIBUTED


def test_first_key_with_a_value_names_the_agent():
    record = CostRecord(
        cloud="aws",
        service="Amazon Bedrock",
        period=Period(start=date(2024, 1, 1), end=date(2024, 1, 1)),
        cost=Money(amount=Decimal("1"), currency="USD"),
        dimensions={"agent": "tokens-in"},
        tags={"agent": "", "app": "support-bot"},
    )

    (report,) = agent_report([record], ("agent", "app"))

    assert report.agent == "support-bot"


def test_split_per_account_and_unattributed_last():
    records = [
        _rec("1.00", "tokens-in"),
        _rec("2.00", "tokens-in", "a"),
        _rec("5.00", "tokens-in", "b"),
        _rec("4.00", "floor", "a", alias="dev"),
    ]

    got = [(r.alias, r.agent, r.total) for r in agent_report(records, ("agent",))]

    assert got == [
        ("dev", "a", Decimal("4.00")),
        ("prod", "b", Decimal("5.00")),
        ("prod", "a", Decimal("2.00")),
        ("prod", UNATTRIBUTED, Decimal("1.00")),
    ]


def test_credits_do_not_break_the_shares():
    records = [_rec("2.00", "floor", "a"), _rec("-2.00", "floor", "a")]

    (report,) = agent_report(records, ("agent",))

    assert report.total == Decimal("0.00")
    assert report.floor_pct == Decimal("0")
