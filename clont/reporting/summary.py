"""Roll one cycle's `Batch` up into a human- or machine-readable summary.

`clont run --summary` is the ad-hoc "did my read-only role actually work?"
path: run one cycle and report what the account looks like, rather than only
paging a channel when something is wrong. This module owns the rollup and its
two renderings; it collects nothing itself.

Money is summed as `Decimal` and grouped by currency — never floats, never a
cross-currency total.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from clont import __version__
from clont.api.serialize import to_jsonable
from clont.api.uplink import Batch
from clont.events.models import EventSeverity
from clont.monitoring.models import HealthStatus

TOP_SPEND_LIMIT = 5


class SummaryFormat(StrEnum):
    JSON = "json"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class MoneyTotal:
    """A total for a single currency (amounts across currencies never mix)."""

    currency: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class ServiceSpend:
    service: str
    currency: str
    amount: Decimal


@dataclass(slots=True)
class ScanSummary:
    """What one cycle saw. Serializable via `to_jsonable`."""

    generated_at: datetime
    version: str
    accounts: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    metrics: int = 0
    costs: int = 0
    recommendations: int = 0
    health_checks: int = 0
    events: int = 0
    events_by_severity: dict[str, int] = field(default_factory=dict)
    events_by_domain: dict[str, int] = field(default_factory=dict)
    health_by_status: dict[str, int] = field(default_factory=dict)
    unhealthy: list[str] = field(default_factory=list)
    estimated_savings: list[MoneyTotal] = field(default_factory=list)
    top_spend: list[ServiceSpend] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def critical_events(self) -> int:
        return self.events_by_severity.get(EventSeverity.CRITICAL.value, 0)


def summarize(
    batch: Batch,
    *,
    accounts: list[str] | None = None,
    duration_seconds: float = 0.0,
    generated_at: datetime | None = None,
) -> ScanSummary:
    """Roll `batch` up into a `ScanSummary`."""
    severity: Counter[str] = Counter(e.severity.value for e in batch.events)
    domain: Counter[str] = Counter(e.domain for e in batch.events)
    status: Counter[str] = Counter(c.status.value for c in batch.health)

    savings: dict[str, Decimal] = defaultdict(Decimal)
    for rec in batch.recommendations:
        savings[rec.estimated_savings.currency] += rec.estimated_savings.amount

    spend: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for record in batch.costs:
        spend[(record.service, record.cost.currency)] += record.cost.amount
    top = sorted(spend.items(), key=lambda kv: kv[1], reverse=True)[:TOP_SPEND_LIMIT]

    return ScanSummary(
        generated_at=generated_at or datetime.now(UTC),
        version=__version__,
        accounts=list(accounts or []),
        duration_seconds=round(duration_seconds, 3),
        metrics=len(batch.metrics),
        costs=len(batch.costs),
        recommendations=len(batch.recommendations),
        health_checks=len(batch.health),
        events=len(batch.events),
        events_by_severity=dict(severity),
        events_by_domain=dict(domain),
        health_by_status=dict(status),
        unhealthy=[
            f"[{c.status.value}] {c.resource.resource_id}: {c.summary}"
            for c in batch.health
            if c.status is not HealthStatus.OK
        ],
        estimated_savings=[
            MoneyTotal(currency=cur, amount=amt) for cur, amt in sorted(savings.items())
        ],
        top_spend=[
            ServiceSpend(service=svc, currency=cur, amount=amt) for (svc, cur), amt in top
        ],
        errors=list(batch.errors),
    )


def render(summary: ScanSummary, fmt: SummaryFormat = SummaryFormat.TEXT) -> str:
    """Render `summary` in the requested format."""
    if fmt is SummaryFormat.JSON:
        return render_json(summary)
    return render_text(summary)


def render_json(summary: ScanSummary) -> str:
    return json.dumps(to_jsonable(summary), indent=2)


def _counts_line(counts: dict[str, int]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"


def render_text(summary: ScanSummary) -> str:
    accounts = ", ".join(summary.accounts) or "none"
    lines = [
        f"clont scan summary ({summary.generated_at.isoformat()}, clont {summary.version})",
        f"accounts:   {accounts}",
        f"duration:   {summary.duration_seconds}s",
        "",
        "collected:",
        f"  metrics={summary.metrics} costs={summary.costs} "
        f"recommendations={summary.recommendations} health={summary.health_checks}",
        "",
        f"events:     {summary.events}",
        f"  by severity: {_counts_line(summary.events_by_severity)}",
        f"  by domain:   {_counts_line(summary.events_by_domain)}",
        "",
        f"health:     {_counts_line(summary.health_by_status)}",
    ]
    lines += [f"  {item}" for item in summary.unhealthy]

    lines += ["", "estimated monthly savings:"]
    lines += [f"  {t.amount} {t.currency}" for t in summary.estimated_savings] or ["  none"]

    lines += ["", f"top spend (up to {TOP_SPEND_LIMIT} services):"]
    lines += [f"  {s.service}: {s.amount} {s.currency}" for s in summary.top_spend] or ["  none"]

    if summary.errors:
        lines += ["", f"errors: {len(summary.errors)} collector failure(s)"]
        lines += [f"  {e}" for e in summary.errors]
    return "\n".join(lines) + "\n"
