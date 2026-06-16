"""Shared helpers for AWS monitoring collectors.

`for_each_region` now lives in the provider layer (it's used by finops too); it
is re-exported here so the monitoring collectors' imports stay unchanged.

`metric_query` / `run_metric_queries` wrap the CloudWatch `GetMetricData`
batching + pagination the metric collectors share (the same shape `finops/idle.py`
uses), so each collector only declares *what* to fetch and how to interpret it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from clont.providers.aws.regions import for_each_region

__all__ = ["for_each_region", "metric_query", "run_metric_queries"]

_MAX_QUERIES = 500  # GetMetricData allows up to 500 queries per call.


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
    cw, queries: list[dict], start: datetime, end: datetime
) -> dict[str, list[tuple[datetime, float]]]:
    """Execute the queries (chunked + paginated) -> ``{query_id: [(ts, value)]}``.

    A metric with no datapoints in the window simply yields an empty list (or is
    absent) — e.g. ``CPUCreditBalance`` on a non-burstable instance — so callers
    can skip it naturally.
    """
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
