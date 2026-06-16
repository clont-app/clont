"""Redshift monitoring: cluster availability via describe_clusters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from clont.core.models import Cloud, CloudResource, Period
from clont.core.registry import register
from clont.monitoring.aws._common import for_each_region, metric_query, run_metric_queries
from clont.monitoring.models import HealthCheck, HealthStatus, MetricPoint
from clont.providers.aws.parsing import _RedshiftCluster
from clont.providers.base import Provider

# Capacity window owned by the collector (see rds.py); 6-hour granularity.
_WINDOW_DAYS = 14
_PERIOD_SECONDS = 21600

# ClusterAvailabilityStatus -> health. Maintenance/Modifying are transient.
_HEALTH = {
    "Available": HealthStatus.OK,
    "Unavailable": HealthStatus.CRITICAL,
    "Failed": HealthStatus.CRITICAL,
    "Maintenance": HealthStatus.UNKNOWN,
    "Modifying": HealthStatus.UNKNOWN,
}


@register("monitoring", Cloud.AWS, "redshift")
class RedshiftHealthCollector:
    cloud = Cloud.AWS
    service = "redshift"

    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    def collect(self, period: Period) -> list[MetricPoint]:
        """Disk-space-used % per cluster (for the disk-full forecast + threshold)."""
        return for_each_region(self._provider, self._region_metrics, what="redshift metrics")

    def _region_metrics(self, region: str) -> list[MetricPoint]:
        rs = self._provider.client("redshift", region)
        cluster_ids = self._cluster_ids(rs)
        if not cluster_ids:
            return []

        end = datetime.now(UTC)
        start = end - timedelta(days=_WINDOW_DAYS)
        queries: list[dict] = []
        id_map: dict[str, str] = {}
        for n, cid in enumerate(cluster_ids):
            qid = f"q{n}"
            id_map[qid] = cid
            # Maximum, not Average: a 6-hour mean can hide a transient near-full
            # peak, which is exactly what the threshold + disk-full forecast exist
            # to catch.
            queries.append(
                metric_query(
                    qid, "AWS/Redshift", "PercentageDiskSpaceUsed",
                    "ClusterIdentifier", cid, _PERIOD_SECONDS, stat="Maximum",
                )
            )

        cw = self._provider.client("cloudwatch", region)
        series = run_metric_queries(cw, queries, start, end)
        points: list[MetricPoint] = []
        for qid, pairs in series.items():
            cid = id_map[qid]
            resource = CloudResource(
                cloud=Cloud.AWS, service="redshift", resource_id=cid,
                region=region, alias=self._provider.alias,
            )
            for ts, val in pairs:
                points.append(MetricPoint("PercentageDiskSpaceUsed", val, "Percent", ts, resource))
        return points

    def _cluster_ids(self, rs) -> list[str]:
        ids: list[str] = []
        for page in rs.get_paginator("describe_clusters").paginate():
            for raw in page.get("Clusters", []):
                ids.append(_RedshiftCluster.model_validate(raw).cluster_id)
        return ids

    def health(self) -> list[HealthCheck]:
        return for_each_region(self._provider, self._region_health, what="redshift health")

    def _region_health(self, region: str) -> list[HealthCheck]:
        rs = self._provider.client("redshift", region)
        checks: list[HealthCheck] = []
        for page in rs.get_paginator("describe_clusters").paginate():
            for raw in page.get("Clusters", []):
                cluster = _RedshiftCluster.model_validate(raw)
                checks.append(
                    HealthCheck(
                        resource=CloudResource(
                            cloud=Cloud.AWS,
                            service="redshift",
                            resource_id=cluster.cluster_id,
                            region=region,
                            alias=self._provider.alias,
                        ),
                        status=_HEALTH.get(cluster.availability, HealthStatus.UNKNOWN),
                        summary=f"availability: {cluster.availability or 'unknown'}",
                    )
                )
        return checks
