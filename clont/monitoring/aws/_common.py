"""Shared helpers for AWS monitoring collectors.

`for_each_region` and the `GetMetricData` plumbing both live in the provider
layer (finops uses them too); they are re-exported here so the monitoring
collectors' imports stay unchanged.

`GetMetricData` is the last paid call clont makes, and it is the one that scales
with the customer's fleet, so `MetricsPolicy` (from `monitoring.base`, re-exported
here) bounds it: an allowlist of metric names and a hard per-cycle ceiling, spent
down as the queries go out.
"""

from __future__ import annotations

from clont.monitoring.base import MetricsPolicy
from clont.providers.aws.metrics import metric_query, run_metric_queries
from clont.providers.aws.regions import for_each_region

__all__ = ["MetricsPolicy", "for_each_region", "metric_query", "run_metric_queries"]
