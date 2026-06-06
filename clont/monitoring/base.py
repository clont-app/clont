"""Metric collector protocol.

Each monitoring service module implements one collector and registers it via
`@register("monitoring", Cloud.X, "service")`.
"""

from __future__ import annotations

from typing import Protocol

from clont.core.models import Cloud, Period
from clont.monitoring.models import HealthCheck, MetricPoint
from clont.providers.base import Provider


class MetricCollector(Protocol):
    cloud: Cloud
    service: str

    def __init__(self, provider: Provider) -> None: ...

    def collect(self, period: Period) -> list[MetricPoint]:
        """Return metric points for the given period."""
        ...

    def health(self) -> list[HealthCheck]:
        """Evaluate current health (may be empty)."""
        ...
