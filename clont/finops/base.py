"""Cost collector protocol.

Each finops service module implements one collector and registers it via
`@register("finops", Cloud.X, "service")`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    ri_sp_min_utilization: float = 90.0    # commitment used below this % -> wasted spend
    ri_sp_min_coverage: float = 70.0       # eligible spend covered below this % -> opportunity
    nonprod_tags: dict[str, tuple[str, ...]] = field(default_factory=dict)  # tag key -> non-prod values
    required_tags: tuple[str, ...] = ()    # tag keys every cost-bearing resource must carry
    allow_cost_explorer: bool = False      # off = the billed ce:GetCostAndUsage is never called
    # off = the CloudWatch idle detectors stay quiet and idle advice comes from
    # Compute Optimizer instead; GetMetricData bills per metric requested
    allow_cloudwatch_metrics: bool = False


class CostCollector(Protocol):
    cloud: Cloud
    service: str

    # How often the agent actually calls this collector. Collectors are
    # duck-typed through `registry` and don't inherit this Protocol, so these
    # are documentation — the runner reads them with getattr and falls back to
    # the operator's finops.collect_interval_seconds / recommend_interval_seconds.
    collect_every_seconds: int
    recommend_every_seconds: int

    def __init__(self, provider: Provider, tuning: FinOpsTuning | None = None) -> None: ...

    def collect(self, period: Period) -> list[CostRecord]:
        """Return cost records for the given period."""
        ...

    def recommendations(self, period: Period) -> list[Recommendation]:
        """Return optimization suggestions (may be empty)."""
        ...
