"""Metric collector protocol + the budget that bounds metric collection.

Each monitoring service module implements one collector and registers it via
`@register("monitoring", Cloud.X, "service")`.

`MetricsPolicy` is the monitoring twin of `FinOpsTuning`: operator-set limits the
agent hands every collector. It exists because `GetMetricData` is the last paid
call clont makes and the only one that scales with the customer's fleet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from clont.core.logging import get_logger
from clont.core.models import Cloud, Period
from clont.monitoring.models import HealthCheck, MetricPoint
from clont.providers.base import Provider

log = get_logger("clont.monitoring")

PER_METRIC_USD = 0.01 / 1000  # GetMetricData: $0.01 per 1,000 metrics requested


@dataclass
class MetricsPolicy:
    """What the metric collectors may ask for this cycle.

    `remaining` is the live budget: the agent resets it at the top of each cycle
    and every batch of queries spends it down, so a fleet that grows past the
    ceiling costs the same as one sitting on it.
    """

    services: frozenset[str] = frozenset()   # empty = every collector that has metrics
    metrics: frozenset[str] = frozenset()    # empty = whatever the collectors ask for
    period_seconds: int | None = None        # override the collectors' own granularity
    max_per_cycle: int = 1000
    remaining: int = field(default=0, init=False)

    def reset(self) -> None:
        self.remaining = self.max_per_cycle

    def allows(self, service: str) -> bool:
        return not self.services or service in self.services

    def trim(self, queries: list[dict]) -> list[dict]:
        """Apply the allowlist, then the budget. Returns what may be sent."""
        if self.metrics:
            queries = [
                q for q in queries
                if q["MetricStat"]["Metric"]["MetricName"] in self.metrics
            ]
        if len(queries) > self.remaining:
            log.warning(
                "cloudwatch metric budget spent: dropping %d of %d queries "
                "(monitoring.metrics.max_metrics_per_cycle=%d)",
                len(queries) - self.remaining, len(queries), self.max_per_cycle,
            )
            queries = queries[: self.remaining]
        self.remaining -= len(queries)
        if self.period_seconds:
            for q in queries:
                q["MetricStat"]["Period"] = self.period_seconds
        log.debug(
            "cloudwatch: %d metrics requested (~$%.4f), %d left this cycle",
            len(queries), len(queries) * PER_METRIC_USD, self.remaining,
        )
        return queries


class MetricCollector(Protocol):
    cloud: Cloud
    service: str

    def __init__(self, provider: Provider, metrics: MetricsPolicy | None = None) -> None: ...

    def collect(self, period: Period) -> list[MetricPoint]:
        """Return metric points for the given period."""
        ...

    def health(self) -> list[HealthCheck]:
        """Evaluate current health (may be empty)."""
        ...
