"""Roll one cycle's `Batch` up into a human- or machine-readable summary.

`clont run --summary` is the ad-hoc "did my read-only role actually work?"
path: run one cycle and report what the account looks like, rather than only
paging a channel when something is wrong. This module owns the rollup and its
renderings; it collects nothing itself.

Three renderings, three audiences: `json` for machines, `text` for the operator
who just ran it, `report` for the person you send it to.

Money is summed as `Decimal` and grouped by currency — never floats, never a
cross-currency total.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from clont import __version__
from clont.api.serialize import to_jsonable
from clont.api.uplink import Batch
from clont.events.models import EventSeverity
from clont.monitoring.models import HealthStatus

TOP_SPEND_LIMIT = 5
FINDING_EXAMPLE_LIMIT = 3
MONTHS_PER_YEAR = 12


class SummaryFormat(StrEnum):
    JSON = "json"
    TEXT = "text"
    REPORT = "report"


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


@dataclass(frozen=True, slots=True)
class FindingExample:
    """One resource behind a finding, so the report names names."""

    resource_id: str
    region: str | None
    amount: Decimal
    summary: str


@dataclass(frozen=True, slots=True)
class Finding:
    """Recommendations of one kind, on one service, in one currency."""

    kind: str
    service: str
    currency: str
    amount: Decimal
    count: int
    examples: list[FindingExample] = field(default_factory=list)


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
    findings: list[Finding] = field(default_factory=list)
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

    grouped: dict[tuple[str, str, str], list] = defaultdict(list)
    for rec in batch.recommendations:
        grouped[(rec.kind, rec.service, rec.estimated_savings.currency)].append(rec)

    findings = []
    for (kind, service, currency), recs in grouped.items():
        # biggest offender first, so the truncated examples are the ones worth seeing
        recs.sort(key=lambda r: r.estimated_savings.amount, reverse=True)
        findings.append(
            Finding(
                kind=kind,
                service=service,
                currency=currency,
                amount=sum((r.estimated_savings.amount for r in recs), Decimal(0)),
                count=len(recs),
                examples=[
                    FindingExample(
                        resource_id=r.resource.resource_id,
                        region=r.resource.region,
                        amount=r.estimated_savings.amount,
                        summary=r.summary,
                    )
                    for r in recs[:FINDING_EXAMPLE_LIMIT]
                ],
            )
        )
    findings.sort(key=lambda f: (-f.amount, f.kind, f.service))

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
        findings=findings,
        top_spend=[
            ServiceSpend(service=svc, currency=cur, amount=amt) for (svc, cur), amt in top
        ],
        errors=list(batch.errors),
    )


def render(summary: ScanSummary, fmt: SummaryFormat = SummaryFormat.TEXT) -> str:
    """Render `summary` in the requested format."""
    if fmt is SummaryFormat.JSON:
        return render_json(summary)
    if fmt is SummaryFormat.REPORT:
        return render_report(summary)
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


_WIDTH = 68
_RULE = "=" * _WIDTH
_THIN = "-" * _WIDTH
_CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}
_UNHEALTHY_LIMIT = 10


def _money(amount: Decimal, currency: str) -> str:
    """always 2dp with thousands separators, symbol when we know one"""
    rounded = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    symbol = _CURRENCY_SYMBOLS.get(currency.upper())
    return f"{symbol}{rounded:,.2f}" if symbol else f"{rounded:,.2f} {currency}"


def _heading(title: str) -> list[str]:
    return ["", _THIN, f" {title.upper()}", _THIN, ""]


def render_report(summary: ScanSummary) -> str:
    """The shareable rendering: the dollar figure first, the evidence under it.

    `render_text` is a debug dump for whoever ran the scan; this one is meant to
    be sent to someone who wasn't there.
    """
    when = summary.generated_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    accounts = ", ".join(summary.accounts) or "none"

    lines = [_RULE, " CLOUD WASTE REPORT", _RULE, ""]

    if summary.estimated_savings:
        lines.append("  you're wasting")
        for total in summary.estimated_savings:
            lines.append(f"    {_money(total.amount, total.currency)} / month")
            lines.append(
                f"    {_money(total.amount * MONTHS_PER_YEAR, total.currency)} / year"
            )
    elif summary.errors:
        # "no waste found" would read as an all-clear; the scan never ran
        lines.append("  nothing to report - this scan did not complete")
    elif not summary.accounts:
        lines.append("  no accounts configured - nothing was scanned")
    else:
        lines.append("  no recoverable waste found in this scan")

    lines += [
        "",
        f"  {summary.recommendations} finding(s) across "
        f"{len(summary.accounts)} account(s): {accounts}",
        f"  scanned {when}  |  clont {summary.version}  |  {summary.duration_seconds}s",
    ]

    # say this next to the number, not in a footnote - it decides whether to trust it
    if summary.errors:
        lines += [
            "",
            f"  !! {len(summary.errors)} collector(s) failed - treat the above as a",
            "     floor, not a total. see COLLECTOR ERRORS below.",
        ]

    if summary.findings:
        lines += _heading("where the money goes")
        pad = max(len(_money(f.amount, f.currency)) for f in summary.findings)
        for finding in summary.findings:
            amount = _money(finding.amount, finding.currency).rjust(pad)
            lines.append(
                f"  {amount}/mo   x{finding.count:<4} {finding.kind} ({finding.service})"
            )
            for example in finding.examples:
                region = f" [{example.region}]" if example.region else ""
                lines.append(
                    f"  {' ' * pad}        - {example.resource_id}{region}  "
                    f"{_money(example.amount, finding.currency)}/mo  {example.summary}"
                )
            hidden = finding.count - len(finding.examples)
            if hidden > 0:
                lines.append(f"  {' ' * pad}        ... and {hidden} more")
            lines.append("")
        lines.pop()

    if summary.top_spend:
        lines += _heading("top spend this window")
        pad = max(len(_money(s.amount, s.currency)) for s in summary.top_spend)
        lines += [
            f"  {_money(s.amount, s.currency).rjust(pad)}   {s.service}"
            for s in summary.top_spend
        ]

    if summary.events or summary.unhealthy:
        lines += _heading("also seen")
        lines.append(
            f"  {summary.events} event(s), {summary.critical_events} critical"
        )
        lines.append(f"  health: {_counts_line(summary.health_by_status)}")
        lines += [f"    {item}" for item in summary.unhealthy[:_UNHEALTHY_LIMIT]]
        hidden = len(summary.unhealthy) - _UNHEALTHY_LIMIT
        if hidden > 0:
            lines.append(f"    ... and {hidden} more")

    if summary.errors:
        lines += _heading("collector errors")
        lines += [f"  {e}" for e in summary.errors]

    lines += [
        "",
        _THIN,
        "  estimates use public on-demand list prices over this scan's window;",
        "  real savings depend on commitments and negotiated rates.",
        "  reproduce: clont run --summary report.txt",
        _THIN,
    ]
    return "\n".join(lines) + "\n"
