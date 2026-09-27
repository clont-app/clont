"""Showback: whose spend is this.

Provider-agnostic on purpose. It groups whatever `CostRecord`s carry tags, so
the same report covers a CUR stream today and an on-prem usage stream later —
the only provider-specific job is filling `CostRecord.tags`.

`tags is None` means the collector knows nothing about tags and is skipped here;
a dict means it does, and a missing or blank value is *unattributed* spend. That
distinction is the whole point: a synthetic run-rate record (public ipv4, say)
must not land in the unattributed bucket and make tag coverage look worse than
it is.

The unattributed line is the number a finops owner is actually asked for — it is
what justifies fixing tags, so it is reported next to the totals, never dropped.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from clont.finops.models import CostRecord

UNATTRIBUTED = "(untagged)"

_CENT = Decimal("0.01")
_TENTH = Decimal("0.1")


@dataclass(frozen=True, slots=True)
class ShowbackLine:
    """One tag value's share of the spend."""

    value: str  # UNATTRIBUTED for the resources that carry no value for the key
    amount: Decimal
    share_pct: Decimal


@dataclass(frozen=True, slots=True)
class ShowbackReport:
    """Spend split by one tag key, for one account and one currency."""

    key: str
    alias: str | None
    currency: str
    start: date
    end: date
    total: Decimal
    unattributed: Decimal
    lines: tuple[ShowbackLine, ...]  # biggest first, unattributed included

    @property
    def unattributed_pct(self) -> Decimal:
        return _pct(self.unattributed, self.total)


def showback(records: list[CostRecord], keys: tuple[str, ...]) -> list[ShowbackReport]:
    """Group tagged spend by each key, per account and currency."""
    if not keys:
        return []
    buckets: dict[tuple[str, str | None, str], dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    window: dict[tuple[str, str | None, str], tuple[date, date]] = {}
    for record in records:
        if record.tags is None:  # collector doesn't do tags -> not our business
            continue
        for key in keys:
            bucket = (key, record.alias, record.cost.currency)
            value = (record.tags.get(key) or "").strip() or UNATTRIBUTED
            buckets[bucket][value] += record.cost.amount
            span = window.get(bucket)
            window[bucket] = (
                min(record.period.start, span[0]) if span else record.period.start,
                max(record.period.end, span[1]) if span else record.period.end,
            )

    reports: list[ShowbackReport] = []
    for (key, alias, currency), values in buckets.items():
        total = sum(values.values(), Decimal(0))
        start, end = window[(key, alias, currency)]
        lines = tuple(
            ShowbackLine(value=v, amount=_money(a), share_pct=_pct(a, total))
            for v, a in sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        reports.append(
            ShowbackReport(
                key=key,
                alias=alias,
                currency=currency,
                start=start,
                end=end,
                total=_money(total),
                unattributed=_money(values.get(UNATTRIBUTED, Decimal(0))),
                lines=lines,
            )
        )
    reports.sort(key=lambda r: (str(r.alias or ""), r.key, r.currency))
    return reports


def _money(amount: Decimal) -> Decimal:
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


def _pct(part: Decimal, total: Decimal) -> Decimal:
    if total <= 0:  # credits can zero or invert a total; no share to report
        return Decimal(0)
    return (part / total * 100).quantize(_TENTH, rounding=ROUND_HALF_UP)
