"""The Event type and severity handling."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from clont.core.models import Cloud, CloudResource


class EventSeverity(StrEnum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


# Ordering for min-severity gating on channels.
_RANK: dict[EventSeverity, int] = {
    EventSeverity.INFO: 0,
    EventSeverity.WARN: 1,
    EventSeverity.CRITICAL: 2,
}


def severity_rank(severity: EventSeverity) -> int:
    return _RANK[severity]


@dataclass(frozen=True, slots=True)
class Event:
    """
    `key` is a *stable* identifier for the underlying condition (not the
    occurrence) so the agent can dedup across cycles and avoid re-alerting on
    the same idle instance every interval.
    """

    key: str
    severity: EventSeverity
    domain: str                       # "finops" | "monitoring"
    cloud: Cloud
    title: str
    message: str
    resource: CloudResource | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
