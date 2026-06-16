"""FinOps idle detection for RDS databases (utilization-based).

A DB instance that has had effectively no connections *and* near-zero CPU over a
trailing window is paying for compute nobody uses — recommend stopping or
removing it. As with idle EC2, the dollar value isn't known (no per-instance
price here), so the saving is left unset and the recommendation leads with the
evidence.

Connections are the primary signal; CPU is a backstop so a DB doing heavy
background work (e.g. vacuum) without client connections isn't called idle.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.aws.idle import _chunks, _mean
from clont.finops.base import FinOpsTuning
from clont.finops.models import CostRecord, Recommendation
from clont.providers.aws.parsing import _RDSInstance
from clont.providers.aws.regions import for_each_region
from clont.providers.base import Provider

_PERIOD = 86400           # 1-day granularity (averaged across the window)
_MAX_QUERIES = 500
_METRICS = ("DatabaseConnections", "CPUUtilization")
_KIND = "idle"


@register("finops", Cloud.AWS, "idle_rds")
class IdleRDSCollector:
    cloud = Cloud.AWS
    service = "idle_rds"

    def __init__(self, provider: Provider, tuning: FinOpsTuning | None = None) -> None:
        self._provider = provider
        self._tuning = tuning or FinOpsTuning()

    def collect(self, period: Period) -> list[CostRecord]:
        return []

    def recommendations(self, period: Period) -> list[Recommendation]:
        return for_each_region(self._provider, self._region, what="idle rds")

    def _region(self, region: str) -> list[Recommendation]:
        rds = self._provider.client("rds", region)
        db_ids = self._available_db_ids(rds)
        if not db_ids:
            return []
        stats = self._db_averages(region, db_ids)
        return [
            self._rec(db_id, region, avg)
            for db_id, avg in stats.items()
            if self._is_idle(avg)
        ]

    def _available_db_ids(self, rds) -> list[str]:
        ids: list[str] = []
        for page in rds.get_paginator("describe_db_instances").paginate():
            for raw in page.get("DBInstances", []):
                db = _RDSInstance.model_validate(raw)
                if db.status == "available":
                    ids.append(db.instance_id)
        return ids

    def _db_averages(self, region: str, db_ids: list[str]) -> dict[str, dict]:
        """Per-DB window-average for each metric -> {db_id: {metric: avg|None}}."""
        end = datetime.now(UTC)
        start = end - timedelta(days=self._tuning.idle_lookback_days)

        queries: list[dict] = []
        id_map: dict[str, tuple[str, str]] = {}
        for n, (db_id, metric) in enumerate(
            (db_id, m) for db_id in db_ids for m in _METRICS
        ):
            qid = f"q{n}"
            id_map[qid] = (db_id, metric)
            queries.append({
                "Id": qid,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/RDS",
                        "MetricName": metric,
                        "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": db_id}],
                    },
                    "Period": _PERIOD,
                    "Stat": "Average",
                },
                "ReturnData": True,
            })

        cw = self._provider.client("cloudwatch", region)
        stats: dict[str, dict] = {db_id: {} for db_id in db_ids}
        for batch in _chunks(queries, _MAX_QUERIES):
            token: str | None = None
            while True:
                kwargs = {"MetricDataQueries": batch, "StartTime": start, "EndTime": end}
                if token:
                    kwargs["NextToken"] = token
                resp = cw.get_metric_data(**kwargs)
                for res in resp.get("MetricDataResults", []):
                    db_id, metric = id_map[res["Id"]]
                    stats[db_id][metric] = _mean(res.get("Values", []))
                token = resp.get("NextToken")
                if not token:
                    break
        return stats

    def _is_idle(self, avg: dict) -> bool:
        conns = avg.get("DatabaseConnections")
        # no data or in use -> not idle
        if conns is None or conns >= self._tuning.idle_rds_max_connections:
            return False
        cpu = avg.get("CPUUtilization")
        return cpu is not None and cpu < self._tuning.idle_cpu_pct

    def _rec(self, db_id: str, region: str, avg: dict) -> Recommendation:
        conns = avg.get("DatabaseConnections") or 0.0
        cpu = avg.get("CPUUtilization") or 0.0
        return Recommendation(
            cloud=str(Cloud.AWS),
            service="rds",
            kind=_KIND,
            resource=CloudResource(
                cloud=Cloud.AWS,
                service="rds",
                resource_id=db_id,
                region=region,
                alias=self._provider.alias,
            ),
            summary=(
                f"Idle {self._tuning.idle_lookback_days}d (avg {conns:.1f} connections, "
                f"avg CPU {cpu:.1f}%) — consider stopping or removing"
            ),
            estimated_savings=Money(amount=Decimal(0)),  # unknown: no per-instance price
        )
