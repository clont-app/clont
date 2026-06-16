"""ElastiCache monitoring: cache cluster health via describe_cache_clusters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from clont.core.models import Cloud, CloudResource, Period
from clont.core.registry import register
from clont.monitoring.aws._common import for_each_region, metric_query, run_metric_queries
from clont.monitoring.models import HealthCheck, HealthStatus, MetricPoint
from clont.providers.aws.parsing import _CacheCluster
from clont.providers.base import Provider

# Pressure window owned by the collector (see rds.py); 6-hour granularity.
_WINDOW_DAYS = 14
_PERIOD_SECONDS = 21600
_MIB = 1_048_576  # SwapUsage / FreeableMemory are bytes; the swap rule is in MB

# ElastiCache CacheClusterStatus -> health. Transient states (creating,
# modifying, snapshotting, rebooting cache cluster nodes) fall through to UNKNOWN.
_HEALTH = {
    "available": HealthStatus.OK,
    "restore-failed": HealthStatus.CRITICAL,
    "incompatible-network": HealthStatus.CRITICAL,
}


@register("monitoring", Cloud.AWS, "elasticache")
class ElastiCacheHealthCollector:
    cloud = Cloud.AWS
    service = "elasticache"

    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    def collect(self, period: Period) -> list[MetricPoint]:
        """Swap usage (MB, for the pressure rule) + freeable memory (anomaly signal)."""
        return for_each_region(self._provider, self._region_metrics, what="elasticache metrics")

    def _region_metrics(self, region: str) -> list[MetricPoint]:
        ec = self._provider.client("elasticache", region)
        cluster_ids = self._available_clusters(ec)
        if not cluster_ids:
            return []

        end = datetime.now(UTC)
        start = end - timedelta(days=_WINDOW_DAYS)
        queries: list[dict] = []
        id_map: dict[str, tuple[str, str]] = {}
        # Worst-case statistic per metric: peak swap (pressure) and trough
        # freeable memory (headroom) — an Average would smooth the spike/dip the
        # detectors are meant to catch.
        _stat = {"SwapUsage": "Maximum", "FreeableMemory": "Minimum"}
        for n, (cid, metric) in enumerate(
            (c, m) for c in cluster_ids for m in ("SwapUsage", "FreeableMemory")
        ):
            qid = f"q{n}"
            id_map[qid] = (cid, metric)
            queries.append(
                metric_query(
                    qid, "AWS/ElastiCache", metric, "CacheClusterId", cid,
                    _PERIOD_SECONDS, stat=_stat[metric],
                )
            )

        cw = self._provider.client("cloudwatch", region)
        series = run_metric_queries(cw, queries, start, end)
        points: list[MetricPoint] = []
        for qid, pairs in series.items():
            cid, metric = id_map[qid]
            resource = CloudResource(
                cloud=Cloud.AWS, service="elasticache", resource_id=cid,
                region=region, alias=self._provider.alias,
            )
            for ts, val in pairs:
                if metric == "SwapUsage":
                    points.append(MetricPoint("SwapUsage", val / _MIB, "Megabytes", ts, resource))
                else:
                    points.append(MetricPoint("FreeableMemory", val, "Bytes", ts, resource))
        return points

    def _available_clusters(self, ec) -> list[str]:
        ids: list[str] = []
        for page in ec.get_paginator("describe_cache_clusters").paginate():
            for raw in page.get("CacheClusters", []):
                cluster = _CacheCluster.model_validate(raw)
                if cluster.status == "available":
                    ids.append(cluster.cluster_id)
        return ids

    def health(self) -> list[HealthCheck]:
        return for_each_region(
            self._provider, self._region_health, what="elasticache health"
        )

    def _region_health(self, region: str) -> list[HealthCheck]:
        ec = self._provider.client("elasticache", region)
        checks: list[HealthCheck] = []
        for page in ec.get_paginator("describe_cache_clusters").paginate():
            for raw in page.get("CacheClusters", []):
                cluster = _CacheCluster.model_validate(raw)
                checks.append(
                    HealthCheck(
                        resource=CloudResource(
                            cloud=Cloud.AWS,
                            service="elasticache",
                            resource_id=cluster.cluster_id,
                            region=region,
                            alias=self._provider.alias,
                        ),
                        status=_HEALTH.get(cluster.status, HealthStatus.UNKNOWN),
                        summary=f"status: {cluster.status}",
                    )
                )
        return checks
