"""Detectors: turn collector output into Events.

Each detector is narrow and pure (inputs -> events), which keeps them easy to
unit-test without touching a cloud. The agent feeds collector results through
the relevant detectors every cycle.
"""

from __future__ import annotations

from clont.events.models import Event, EventSeverity
from clont.finops.models import Recommendation
from clont.monitoring.models import HealthCheck, HealthStatus


class RecommendationDetector:
    """FinOps cost recommendations -> one event each."""

    def detect(self, recommendations: list[Recommendation]) -> list[Event]:
        events: list[Event] = []
        for rec in recommendations:
            r = rec.resource
            account = r.account or "-"
            events.append(
                Event(
                    key=f"finops:rec:{account}:{r.cloud}:{r.service}:{r.resource_id}",
                    severity=EventSeverity.WARN,
                    domain="finops",
                    cloud=r.cloud,
                    title=f"[{account}] Cost recommendation: {rec.service}",
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
            account = r.account or "-"
            events.append(
                Event(
                    key=f"monitoring:health:{account}:{r.cloud}:{r.service}:{r.resource_id}",
                    severity=severity,
                    domain="monitoring",
                    cloud=r.cloud,
                    title=f"[{account}] Health {chk.status}: {r.service}",
                    message=chk.summary,
                    resource=r,
                )
            )
        return events
