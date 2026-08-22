"""CloudWatch `GetMetricData` plumbing, shared by monitoring and finops.

Lives in the provider layer for the same reason `for_each_region` does: both
stacks need it, and finops shouldn't reach into a monitoring private module.
Callers declare *what* to fetch; batching and pagination happen here.

This is the last paid call clont makes and it bills per *metric requested*, so a
caller may hand in a budget (`monitoring.base.MetricsPolicy`) that trims the
queries before they go out.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Protocol

_MAX_QUERIES = 500  # per-call limit AWS enforces; billing is per *metric*, not per call


class QueryBudget(Protocol):
    def trim(self, queries: list[dict]) -> list[dict]: ...


def _chunks[T](items: list[T], size: int) -> Iterator[list[T]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def metric_query(
    qid: str, namespace: str, name: str, dim_name: str, dim_value: str,
    period_seconds: int, stat: str = "Average",
) -> dict:
    """One CloudWatch ``MetricDataQuery`` for a single resource's metric."""
    return {
        "Id": qid,
        "MetricStat": {
            "Metric": {
                "Namespace": namespace,
                "MetricName": name,
                "Dimensions": [{"Name": dim_name, "Value": dim_value}],
            },
            "Period": period_seconds,
            "Stat": stat,
        },
        "ReturnData": True,
    }


def run_metric_queries(
    cw,
    queries: list[dict],
    start: datetime,
    end: datetime,
    policy: QueryBudget | None = None,
) -> dict[str, list[tuple[datetime, float]]]:
    """Execute the queries (chunked + paginated) -> ``{query_id: [(ts, value)]}``.

    A metric with no datapoints in the window simply yields an empty list (or is
    absent) — e.g. ``CPUCreditBalance`` on a non-burstable instance — so callers
    can skip it naturally.
    """
    if policy is not None:
        queries = policy.trim(queries)
    out: dict[str, list[tuple[datetime, float]]] = {}
    for batch in _chunks(queries, _MAX_QUERIES):
        token: str | None = None
        while True:
            kwargs = {"MetricDataQueries": batch, "StartTime": start, "EndTime": end}
            if token:
                kwargs["NextToken"] = token
            resp = cw.get_metric_data(**kwargs)
            for res in resp.get("MetricDataResults", []):
                pairs = out.setdefault(res["Id"], [])
                for ts, val in zip(
                    res.get("Timestamps", []), res.get("Values", []), strict=False
                ):
                    pairs.append((ts, float(val)))
            token = resp.get("NextToken")
            if not token:
                break
    return out


def metric_values(
    series: dict[str, list[tuple[datetime, float]]], qid: str
) -> list[float]:
    """Just the values for one query id — callers that don't care about time."""
    return [value for _, value in series.get(qid, [])]
