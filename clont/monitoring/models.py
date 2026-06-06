"""Monitoring result types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from clont.core.models import CloudResource


@dataclass(frozen=True, slots=True)
class MetricPoint:
    """A single timestamped metric value."""

    name: str            # e.g. "CPUUtilization"
    value: float
    unit: str
    timestamp: datetime
    resource: CloudResource | None = None


class HealthStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """Evaluated health of a resource against some condition."""

    resource: CloudResource
    status: HealthStatus
    summary: str
