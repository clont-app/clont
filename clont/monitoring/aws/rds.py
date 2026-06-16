"""RDS monitoring: DB instance lifecycle health via describe_db_instances."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from clont.core.models import Cloud, CloudResource, Period
from clont.core.registry import register
from clont.monitoring.aws._common import for_each_region, metric_query, run_metric_queries
from clont.monitoring.models import HealthCheck, HealthStatus, MetricPoint
from clont.providers.aws.parsing import _RDSInstance
from clont.providers.base import Provider

# Capacity/forecast metrics need weeks of history for a usable trend; this window
# is owned by the collector (the idle.py precedent), independent of the cycle's
# short metric `period`. 6-hour granularity -> ~56 points over the window.
_WINDOW_DAYS = 14
_PERIOD_SECONDS = 21600  # 6 hours
_GIB = 1_073_741_824     # FreeStorageSpace is bytes; AllocatedStorage is GiB

# RDS DBInstanceStatus -> health. Anything not listed (creating, modifying,
# backing-up, rebooting, ...) is transient -> UNKNOWN, which the detector ignores.
_HEALTH = {
    "available": HealthStatus.OK,
    "stopped": HealthStatus.OK,  # intentional, not a fault
    "storage-full": HealthStatus.CRITICAL,
    "failed": HealthStatus.CRITICAL,
    "incompatible-credentials": HealthStatus.CRITICAL,
    "incompatible-network": HealthStatus.CRITICAL,
    "incompatible-option-group": HealthStatus.CRITICAL,
    "incompatible-parameters": HealthStatus.CRITICAL,
    "incompatible-restore": HealthStatus.CRITICAL,
    "restore-error": HealthStatus.CRITICAL,
    "inaccessible-encryption-credentials": HealthStatus.CRITICAL,
    "inaccessible-encryption-credentials-recoverable": HealthStatus.WARN,
}


@register("monitoring", Cloud.AWS, "rds")
class RDSHealthCollector:
    cloud = Cloud.AWS
    service = "rds"

    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    def collect(self, period: Period) -> list[MetricPoint]:
        """Free-storage % (for disk-full forecast) + CPU-credit balance per DB."""
        return for_each_region(self._provider, self._region_metrics, what="rds metrics")

    def _region_metrics(self, region: str) -> list[MetricPoint]:
        rds = self._provider.client("rds", region)
        dbs = self._available_dbs(rds)  # {db_id: allocated_gib, 0 = skip free-storage}
        if not dbs:
            return []

        end = datetime.now(UTC)
        start = end - timedelta(days=_WINDOW_DAYS)
        queries: list[dict] = []
        id_map: dict[str, tuple[str, int, str]] = {}
        for n, ((db_id, alloc), metric) in enumerate(
            (db, m) for db in dbs.items() for m in ("FreeStorageSpace", "CPUCreditBalance")
        ):
            qid = f"q{n}"
            id_map[qid] = (db_id, alloc, metric)
            # Worst-case statistic: the low point of free storage / CPU credits in
            # each bucket is what the threshold + disk-full detectors care about; a
            # 6-hour Average would smooth a transient near-empty dip away.
            queries.append(
                metric_query(
                    qid, "AWS/RDS", metric, "DBInstanceIdentifier", db_id,
                    _PERIOD_SECONDS, stat="Minimum",
                )
            )

        cw = self._provider.client("cloudwatch", region)
        series = run_metric_queries(cw, queries, start, end)
        points: list[MetricPoint] = []
        for qid, pairs in series.items():
            db_id, alloc, metric = id_map[qid]
            resource = CloudResource(
                cloud=Cloud.AWS, service="rds", resource_id=db_id,
                region=region, alias=self._provider.alias,
            )
            for ts, val in pairs:
                if metric == "FreeStorageSpace":
                    # alloc == 0 means "can't / shouldn't normalize" — either the
                    # allocated size is unknown or storage autoscaling is on (the
                    # disk grows, so a disk-full forecast would be a false alarm).
                    if alloc <= 0:
                        continue
                    pct = val / (alloc * _GIB) * 100
                    points.append(MetricPoint("FreeStoragePercent", pct, "Percent", ts, resource))
                else:
                    points.append(MetricPoint("CPUCreditBalance", val, "Count", ts, resource))
        return points

    def _available_dbs(self, rds) -> dict[str, int]:
        """Map of available DB instance id -> allocated storage (GiB).

        An instance with storage autoscaling enabled is mapped to ``0`` so the
        free-storage forecast is skipped for it (its disk grows automatically, so
        % free against the current allocation would fire a false disk-full alert)
        while its CPU-credit signal is still collected.
        """
        dbs: dict[str, int] = {}
        for page in rds.get_paginator("describe_db_instances").paginate():
            for raw in page.get("DBInstances", []):
                db = _RDSInstance.model_validate(raw)
                if db.status != "available":
                    continue
                autoscaling = db.max_allocated_storage > db.allocated_storage
                dbs[db.instance_id] = 0 if autoscaling else db.allocated_storage
        return dbs

    def health(self) -> list[HealthCheck]:
        return for_each_region(self._provider, self._region_health, what="rds health")

    def _region_health(self, region: str) -> list[HealthCheck]:
        rds = self._provider.client("rds", region)
        checks: list[HealthCheck] = []
        for page in rds.get_paginator("describe_db_instances").paginate():
            for raw in page.get("DBInstances", []):
                db = _RDSInstance.model_validate(raw)
                checks.append(
                    HealthCheck(
                        resource=CloudResource(
                            cloud=Cloud.AWS,
                            service="rds",
                            resource_id=db.instance_id,
                            region=region,
                            alias=self._provider.alias,
                        ),
                        status=_HEALTH.get(db.status, HealthStatus.UNKNOWN),
                        summary=f"status: {db.status}",
                    )
                )
        return checks
