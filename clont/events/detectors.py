"""Detectors: turn collector output into Events.

Each detector is narrow and pure (inputs -> events), which keeps them easy to
unit-test without touching a cloud. The agent feeds collector results through
the relevant detectors every cycle.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from clont.core.models import Cloud, CloudResource
from clont.events.models import Event, EventSeverity
from clont.finops.models import CostRecord, Recommendation
from clont.monitoring.models import HealthCheck, HealthStatus


class RecommendationDetector:
    """FinOps cost recommendations -> one event each."""

    def detect(self, recommendations: list[Recommendation]) -> list[Event]:
        events: list[Event] = []
        for rec in recommendations:
            r = rec.resource
            alias = r.alias or "-"
            events.append(
                Event(
                    key=f"finops:rec:{alias}:{r.cloud}:{r.service}:{r.resource_id}",
                    severity=EventSeverity.WARN,
                    domain="finops",
                    cloud=r.cloud,
                    title=f"[{alias}] Cost recommendation: {rec.service}",
                    message=(
                        f"{rec.summary} "
                        f"(est. savings {rec.estimated_savings.amount} "
                        f"{rec.estimated_savings.currency})"
                    ),
                    resource=r,
                    payload={
                        "estimated_savings": str(rec.estimated_savings.amount),
                        "currency": rec.estimated_savings.currency,
                    },
                )
            )
        return events


class SpendDigestDetector:
    """Daily spend -> one INFO digest event per account (latest day present).
    """

    _TOP_N = 5

    def detect(self, records: list[CostRecord]) -> list[Event]:
        by_alias: dict[str | None, list[CostRecord]] = defaultdict(list)
        for rec in records:
            by_alias[rec.alias].append(rec)

        events: list[Event] = []
        for alias_key, recs in by_alias.items():
            latest_day = max(r.period.end for r in recs)
            today = [r for r in recs if r.period.end == latest_day]
            total = sum((r.cost.amount for r in today), Decimal(0))
            currency = today[0].cost.currency
            alias = alias_key or "-"

            top = sorted(today, key=lambda r: r.cost.amount, reverse=True)[: self._TOP_N]
            breakdown = ", ".join(f"{r.service} {r.cost.amount}" for r in top)
            events.append(
                Event(
                    key=f"finops:spend:digest:{alias}",
                    severity=EventSeverity.INFO,
                    domain="finops",
                    cloud=Cloud.AWS,
                    title=f"[{alias}] Daily spend: {total} {currency}",
                    message=f"{latest_day}: {total} {currency} — top: {breakdown}",
                    payload={
                        "day": latest_day.isoformat(),
                        "total": str(total),
                        "currency": currency,
                        "services": {r.service: str(r.cost.amount) for r in today},
                    },
                )
            )
        return events


class SpendSpikeDetector:
    """Daily spend -> WARN when a service's latest day jumps over its baseline.

    The "latest day" is the most recent day in the dataset for the *account*
    (not per service), so a service that has gone quiet can't keep alerting on a
    stale day. A service with no prior days (new account / short window) can't spike.
    """

    def __init__(self, spike_pct: float = 50.0, min_dollars: float = 1.0) -> None:
        self._factor = Decimal(1) + Decimal(str(spike_pct)) / Decimal(100)
        self._min = Decimal(str(min_dollars))

    def detect(self, records: list[CostRecord]) -> list[Event]:
        by_alias: dict[str | None, list[CostRecord]] = defaultdict(list)
        for rec in records:
            by_alias[rec.alias].append(rec)

        events: list[Event] = []
        for alias_key, recs in by_alias.items():
            anchor = max(r.period.end for r in recs)  # latest day for this account
            series: dict[str, dict] = defaultdict(dict)
            currency: dict[str, str] = {}
            for rec in recs:
                day = rec.period.end
                series[rec.service][day] = series[rec.service].get(day, Decimal(0)) + rec.cost.amount
                currency[rec.service] = rec.cost.currency

            for service, by_day in series.items():
                latest = by_day.get(anchor)
                if latest is None:  # service had no spend on the anchor day
                    continue
                prior = [amt for day, amt in by_day.items() if day < anchor]
                if not prior:
                    continue
                baseline = sum(prior, Decimal(0)) / len(prior)
                if baseline <= 0 or latest < self._min or latest <= baseline * self._factor:
                    continue

                alias = alias_key or "-"
                pct = (latest - baseline) / baseline * 100
                cur = currency[service]
                events.append(
                    Event(
                        key=f"finops:spend:spike:{alias}:aws:{service}",
                        severity=EventSeverity.WARN,
                        domain="finops",
                        cloud=Cloud.AWS,
                        title=f"[{alias}] Spend spike: {service}",
                        message=f"{latest} {cur} vs {baseline:.2f} avg (+{pct:.0f}%)",
                        resource=CloudResource(
                            cloud=Cloud.AWS,
                            service=service,
                            resource_id=service,
                            alias=alias_key,
                        ),
                        payload={
                            "latest": str(latest),
                            "baseline": str(baseline),
                            "currency": cur,
                        },
                    )
                )
        return events


class HealthDetector:
    """Monitoring health checks -> events for anything not OK."""

    _SEVERITY = {
        HealthStatus.WARN: EventSeverity.WARN,
        HealthStatus.CRITICAL: EventSeverity.CRITICAL,
    }

    def detect(self, checks: list[HealthCheck]) -> list[Event]:
        events: list[Event] = []
        for chk in checks:
            severity = self._SEVERITY.get(chk.status)
            if severity is None:  # OK / UNKNOWN -> not an event
                continue
            r = chk.resource
            alias = r.alias or "-"
            events.append(
                Event(
                    key=f"monitoring:health:{alias}:{r.cloud}:{r.service}:{r.resource_id}",
                    severity=severity,
                    domain="monitoring",
                    cloud=r.cloud,
                    title=f"[{alias}] Health {chk.status}: {r.service}",
                    message=chk.summary,
                    resource=r,
                )
            )
        return events
