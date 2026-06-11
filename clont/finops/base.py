"""Cost collector protocol.

Each finops service module implements one collector and registers it via
`@register("finops", Cloud.X, "service")`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from clont.core.models import Cloud, Period
from clont.finops.models import CostRecord, Recommendation
from clont.providers.base import Provider


@dataclass(frozen=True, slots=True)
class FinOpsTuning:
    """Operator-tunable thresholds shared across finops collectors.

    """

    idle_cpu_pct: float = 5.0              # avg CPU % below which a resource is idle
    idle_lookback_days: int = 14           # trailing window the averages span
    idle_rds_max_connections: float = 1.0  # avg DB connections below which RDS is idle
    snapshot_max_age_days: int = 90        # snapshots older than this are "old"


class CostCollector(Protocol):
    cloud: Cloud
    service: str

    def __init__(self, provider: Provider, tuning: FinOpsTuning | None = None) -> None: ...

    def collect(self, period: Period) -> list[CostRecord]:
        """Return cost records for the given period."""
        ...

    def recommendations(self, period: Period) -> list[Recommendation]:
        """Return optimization suggestions (may be empty)."""
        ...
