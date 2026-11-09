"""Agent spend: what an ai agent cost, all in, read off the bill.

Provider-agnostic like showback and transfer. It reads
`CostRecord.dimensions["agent"]` (the bucket) and `CostRecord.tags` (which
agent), so an on-prem or k8s stream gets the same report the day it labels its
records.

No prices. Every number is a bill line; this only adds them up. The headline is
the floor share: the part of the agent's bill that runs whether anyone calls it
or not, restated as a monthly run-rate.

Agent spend with no agent tag is its own line, `UNATTRIBUTED`, never spread over
the tagged agents: it is the number that justifies tagging the role or the
inference profile.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from clont.finops.models import CostRecord

DIMENSION = "agent"
UNATTRIBUTED = "(unattributed)"

TOKENS = ("tokens-in", "tokens-out", "cache-read", "cache-write")
FLOOR = "floor"
PLUMBING = "plumbing"

_MONTH_DAYS = Decimal(30)
_CENT = Decimal("0.01")
_TENTH = Decimal("0.1")


@dataclass(frozen=True, slots=True)
class AgentLine:
    """One bucket's share of an agent's bill."""

    bucket: str
    amount: Decimal
    share_pct: Decimal


@dataclass(frozen=True, slots=True)
class AgentReport:
    """One agent's spend for one account and currency."""

    agent: str  # UNATTRIBUTED for agent spend that carries no agent tag
    alias: str | None
    currency: str
    start: date
    end: date
    total: Decimal
    tokens: Decimal
    floor: Decimal
    plumbing: Decimal
    lines: tuple[AgentLine, ...]  # biggest first
    monthly_floor_run_rate: Decimal
    cache_read_share: Decimal  # % of input-side token spend served from cache

    @property
    def floor_pct(self) -> Decimal:
        return _pct(self.floor, self.total)


def agent_report(records: list[CostRecord], tag_keys: tuple[str, ...]) -> list[AgentReport]:
    """Group agent spend per agent, account and currency."""
    if not tag_keys:
        return []
    buckets: dict[tuple[str, str | None, str], dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    window: dict[tuple[str, str | None, str], tuple[date, date]] = {}
    for record in records:
        bucket = ((record.dimensions or {}).get(DIMENSION) or "").strip()
        if not bucket:
            continue
        key = (_agent(record, tag_keys), record.alias, record.cost.currency)
        buckets[key][bucket] += record.cost.amount
        span = window.get(key)
        window[key] = (
            min(record.period.start, span[0]) if span else record.period.start,
            max(record.period.end, span[1]) if span else record.period.end,
        )

    reports: list[AgentReport] = []
    for key, values in buckets.items():
        agent, alias, currency = key
        start, end = window[key]
        total = sum(values.values(), Decimal(0))
        floor = values.get(FLOOR, Decimal(0))
        input_side = values["tokens-in"] + values["cache-read"] + values["cache-write"]
        days = Decimal((end - start).days + 1)
        reports.append(
            AgentReport(
                agent=agent,
                alias=alias,
                currency=currency,
                start=start,
                end=end,
                total=_money(total),
                tokens=_money(sum((values.get(b, Decimal(0)) for b in TOKENS), Decimal(0))),
                floor=_money(floor),
                plumbing=_money(values.get(PLUMBING, Decimal(0))),
                lines=tuple(
                    AgentLine(bucket=b, amount=_money(a), share_pct=_pct(a, total))
                    for b, a in sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))
                    if a
                ),
                monthly_floor_run_rate=_money(floor / days * _MONTH_DAYS),
                cache_read_share=_pct(values["cache-read"], input_side),
            )
        )
    # unattributed last in each account: it is the to-do, not an agent
    reports.sort(
        key=lambda r: (str(r.alias or ""), r.currency, r.agent == UNATTRIBUTED, -r.total, r.agent)
    )
    return reports


def _agent(record: CostRecord, keys: tuple[str, ...]) -> str:
    tags = record.tags or {}
    for key in keys:
        value = (tags.get(key) or "").strip()
        if value:
            return value
    return UNATTRIBUTED


def _money(amount: Decimal) -> Decimal:
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


def _pct(part: Decimal, total: Decimal) -> Decimal:
    if total <= 0:  # credits can zero or invert a total; no share to report
        return Decimal(0)
    return (part / total * 100).quantize(_TENTH, rounding=ROUND_HALF_UP)
