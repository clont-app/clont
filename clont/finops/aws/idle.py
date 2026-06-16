"""FinOps idle detection from CloudWatch metrics (utilization-based).

An EC2 instance that has sat at near-zero CPU *and* near-zero network over a
trailing window is almost certainly wasted spend — recommend stopping it. Unlike
the waste checks, the dollar value isn't known here (we have no per-instance
price), so the saving is left unset and the recommendation leads with the
evidence instead.

The CPU threshold and lookback window come from `FinOpsTuning` (operator config);
the network guard stays a fixed backstop. `idle_rds` reuses this machinery with
AWS/RDS metrics.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.base import FinOpsTuning
from clont.finops.models import CostRecord, Recommendation
from clont.providers.aws.regions import for_each_region
from clont.providers.base import Provider

_PERIOD = 86400              # 1-day granularity (averaged across the window)
# Backstop only: avg bytes per CloudWatch sample summed over NetworkIn+Out. CPU
# is the real signal; this just keeps a busy-but-low-CPU box (e.g. a pure I/O
# mover) from being called idle. Deliberately lenient.
_NET_GUARD_BYTES = 5_000_000
_MAX_QUERIES = 500
_METRICS = ("CPUUtilization", "NetworkIn", "NetworkOut")
_KIND = "idle"


def _chunks[T](items: list[T], size: int) -> Iterator[list[T]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


@register("finops", Cloud.AWS, "idle")
class IdleCollector:
    cloud = Cloud.AWS
    service = "idle"

    def __init__(self, provider: Provider, tuning: FinOpsTuning | None = None) -> None:
        self._provider = provider
        self._tuning = tuning or FinOpsTuning()

    def collect(self, period: Period) -> list[CostRecord]:
        return []

    def recommendations(self, period: Period) -> list[Recommendation]:
        return for_each_region(self._provider, self._region, what="idle ec2")

    def _region(self, region: str) -> list[Recommendation]:
        ec2 = self._provider.client("ec2", region)
        instance_ids = self._running_instance_ids(ec2)
        if not instance_ids:
            return []
        stats = self._instance_averages(region, instance_ids)
        return [
            self._rec(iid, region, avg)
            for iid, avg in stats.items()
            if self._is_idle(avg)
        ]

    def _running_instance_ids(self, ec2) -> list[str]:
        ids: list[str] = []
        for page in ec2.get_paginator("describe_instance_status").paginate():
            ids.extend(s["InstanceId"] for s in page.get("InstanceStatuses", []))
        return ids

    def _instance_averages(self, region: str, instance_ids: list[str]) -> dict[str, dict]:
        """Per-instance window-average for each metric -> {iid: {metric: avg|None}}."""
        end = datetime.now(UTC)
        start = end - timedelta(days=self._tuning.idle_lookback_days)

        queries: list[dict] = []
        id_map: dict[str, tuple[str, str]] = {}
        for n, (iid, metric) in enumerate(
            (iid, m) for iid in instance_ids for m in _METRICS
        ):
            qid = f"q{n}"
            id_map[qid] = (iid, metric)
            queries.append({
                "Id": qid,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/EC2",
                        "MetricName": metric,
                        "Dimensions": [{"Name": "InstanceId", "Value": iid}],
                    },
                    "Period": _PERIOD,
                    "Stat": "Average",
                },
                "ReturnData": True,
            })

        cw = self._provider.client("cloudwatch", region)
        stats: dict[str, dict] = {iid: {} for iid in instance_ids}
        for batch in _chunks(queries, _MAX_QUERIES):
            token: str | None = None
            while True:
                kwargs = {"MetricDataQueries": batch, "StartTime": start, "EndTime": end}
                if token:
                    kwargs["NextToken"] = token
                resp = cw.get_metric_data(**kwargs)
                for res in resp.get("MetricDataResults", []):
                    iid, metric = id_map[res["Id"]]
                    stats[iid][metric] = _mean(res.get("Values", []))
                token = resp.get("NextToken")
                if not token:
                    break
        return stats

    def _is_idle(self, avg: dict) -> bool:
        cpu = avg.get("CPUUtilization")
        if cpu is None or cpu >= self._tuning.idle_cpu_pct:  # no data or busy -> not idle
            return False
        net = (avg.get("NetworkIn") or 0.0) + (avg.get("NetworkOut") or 0.0)
        return net < _NET_GUARD_BYTES

    def _rec(self, iid: str, region: str, avg: dict) -> Recommendation:
        cpu = avg.get("CPUUtilization") or 0.0
        net = (avg.get("NetworkIn") or 0.0) + (avg.get("NetworkOut") or 0.0)
        return Recommendation(
            cloud=str(Cloud.AWS),
            service="ec2",
            kind=_KIND,
            resource=CloudResource(
                cloud=Cloud.AWS,
                service="ec2",
                resource_id=iid,
                region=region,
                alias=self._provider.alias,
            ),
            summary=(
                f"Idle {self._tuning.idle_lookback_days}d (avg CPU {cpu:.1f}%, "
                f"avg net {net / 1000:.0f} KB/sample) — consider stopping"
            ),
            estimated_savings=Money(amount=Decimal(0)),  # unknown: no per-instance price
        )
