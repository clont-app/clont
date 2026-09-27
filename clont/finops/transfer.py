"""Data transfer: which network dollars, and how big a share of the bill.

Provider-agnostic like showback. It reads `CostRecord.dimensions["transfer"]`, so
the only provider-specific job is putting a bucket there — cur does it from
`lineItem/UsageType` today, an on-prem link/egress stream can do it later.

The share is computed against *all* records, not just the transfer ones: "9% of
your bill is network" is the sentence that gets someone to look, and a bucket
breakdown on its own can't say it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from clont.finops.models import CostRecord

DIMENSION = "transfer"

_CENT = Decimal("0.01")
_TENTH = Decimal("0.1")


@dataclass(frozen=True, slots=True)
class TransferLine:
    """One transfer bucket's spend, and its share of transfer spend."""

    bucket: str
    amount: Decimal
    share_pct: Decimal
    services: tuple[str, ...]  # biggest first, the top talkers in this bucket


@dataclass(frozen=True, slots=True)
class TransferReport:
    """Data transfer spend for one account and currency."""

    alias: str | None
    currency: str
    start: date
    end: date
    total: Decimal  # every dollar in the window, transfer or not
    transfer: Decimal
    lines: tuple[TransferLine, ...]  # biggest bucket first

    @property
    def transfer_pct(self) -> Decimal:
        return _pct(self.transfer, self.total)


def transfer_report(records: list[CostRecord], top_services: int = 3) -> list[TransferReport]:
    """Group transfer spend per account and currency."""
    totals: dict[tuple[str | None, str], Decimal] = defaultdict(Decimal)
    buckets: dict[tuple[str | None, str], dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    talkers: dict[tuple[str | None, str, str], dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    window: dict[tuple[str | None, str], tuple[date, date]] = {}

    for record in records:
        key = (record.alias, record.cost.currency)
        totals[key] += record.cost.amount
        span = window.get(key)
        window[key] = (
            min(record.period.start, span[0]) if span else record.period.start,
            max(record.period.end, span[1]) if span else record.period.end,
        )
        bucket = ((record.dimensions or {}).get(DIMENSION) or "").strip()
        if not bucket:
            continue
        buckets[key][bucket] += record.cost.amount
        talkers[(*key, bucket)][record.service] += record.cost.amount

    reports: list[TransferReport] = []
    for key, values in buckets.items():
        alias, currency = key
        transfer = sum(values.values(), Decimal(0))
        start, end = window[key]
        lines = tuple(
            TransferLine(
                bucket=bucket,
                amount=_money(amount),
                share_pct=_pct(amount, transfer),
                services=_top(talkers[(*key, bucket)], top_services),
            )
            for bucket, amount in sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        reports.append(
            TransferReport(
                alias=alias,
                currency=currency,
                start=start,
                end=end,
                total=_money(totals[key]),
                transfer=_money(transfer),
                lines=lines,
            )
        )
    reports.sort(key=lambda r: (str(r.alias or ""), r.currency))
    return reports


def _top(services: dict[str, Decimal], limit: int) -> tuple[str, ...]:
    ordered = sorted(services.items(), key=lambda kv: (-kv[1], kv[0]))
    return tuple(name for name, _ in ordered[:limit])


def _money(amount: Decimal) -> Decimal:
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


def _pct(part: Decimal, total: Decimal) -> Decimal:
    if total <= 0:  # credits can zero or invert a total; no share to report
        return Decimal(0)
    return (part / total * 100).quantize(_TENTH, rounding=ROUND_HALF_UP)
