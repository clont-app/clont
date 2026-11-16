"""finops idle detection for elasticache: a cache nobody reads or writes.

`CurrConnections` can't decide this — elasticache keeps a few connections of its
own open to monitor every node, so an idle cache never reads zero. the command
counters do: an idle cache serves no get and no set over the whole window.
`INFO`/`PING` from monitoring aren't counted there.

nodes of one replication group are summed and judged as one: readers may only
hit replicas, and "remove the primary" alone is wrong advice.

off unless `finops.allow_cloudwatch_metrics` is set, same trade-off as `idle.py`.
compute optimizer doesn't cover elasticache, so there is no free path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.base import FinOpsTuning
from clont.finops.models import CostRecord, Recommendation
from clont.providers.aws.metrics import metric_query, metric_values, run_metric_queries
from clont.providers.aws.regions import for_each_region
from clont.providers.base import Provider

_PERIOD = 86400
_KIND = "idle"
# reads, writes per engine. valkey speaks the redis metric names
_CMDS = {
    "redis": ("GetTypeCmds", "SetTypeCmds"),
    "valkey": ("GetTypeCmds", "SetTypeCmds"),
    "memcached": ("CmdGet", "CmdSet"),
}


@register("finops", Cloud.AWS, "idle_elasticache")
class IdleElastiCacheCollector:
    cloud = Cloud.AWS
    service = "idle_elasticache"
    recommend_every_seconds = 86400

    def __init__(self, provider: Provider, tuning: FinOpsTuning | None = None) -> None:
        self._provider = provider
        self._tuning = tuning or FinOpsTuning()

    def collect(self, period: Period) -> list[CostRecord]:
        return []

    def recommendations(self, period: Period) -> list[Recommendation]:
        if not self._tuning.allow_cloudwatch_metrics:
            return []
        return for_each_region(self._provider, self._region, what="idle elasticache")

    def _region(self, region: str) -> list[Recommendation]:
        ec = self._provider.client("elasticache", region)
        groups = self._groups(ec)
        if not groups:
            return []
        totals = self._totals(region, groups)
        return [
            self._rec(group_id, region, engine, len(nodes))
            for group_id, (engine, nodes) in groups.items()
            if totals.get(group_id) == 0.0
        ]

    def _groups(self, ec) -> dict[str, tuple[str, list[str]]]:
        """{replication group or cluster id: (engine, [cache cluster ids])}"""
        groups: dict[str, tuple[str, list[str]]] = {}
        for page in ec.get_paginator("describe_cache_clusters").paginate():
            for c in page.get("CacheClusters", []):
                engine = c.get("Engine", "")
                if c.get("CacheClusterStatus") != "available" or engine not in _CMDS:
                    continue
                cid = c["CacheClusterId"]
                gid = c.get("ReplicationGroupId") or cid
                groups.setdefault(gid, (engine, []))[1].append(cid)
        return groups

    def _totals(self, region: str, groups: dict) -> dict[str, float | None]:
        """commands served per group over the window, None when a node has no data"""
        end = datetime.now(UTC)
        start = end - timedelta(days=self._tuning.idle_lookback_days)

        queries: list[dict] = []
        id_map: dict[str, str] = {}
        for gid, (engine, nodes) in groups.items():
            for cid in nodes:
                for metric in _CMDS[engine]:
                    qid = f"q{len(queries)}"
                    id_map[qid] = gid
                    queries.append(
                        metric_query(
                            qid, "AWS/ElastiCache", metric, "CacheClusterId", cid,
                            _PERIOD, stat="Sum",
                        )
                    )

        cw = self._provider.client("cloudwatch", region)
        series = run_metric_queries(cw, queries, start, end)
        totals: dict[str, float | None] = {gid: 0.0 for gid in groups}
        for qid, gid in id_map.items():
            values = metric_values(series, qid)
            # a node with no datapoints at all proves nothing, keep the group
            if not values or totals[gid] is None:
                totals[gid] = None
                continue
            totals[gid] += sum(values)
        return totals

    def _rec(self, group_id: str, region: str, engine: str, nodes: int) -> Recommendation:
        days = self._tuning.idle_lookback_days
        return Recommendation(
            cloud=str(Cloud.AWS),
            service="elasticache",
            kind=_KIND,
            resource=CloudResource(
                cloud=Cloud.AWS,
                service="elasticache",
                resource_id=group_id,
                region=region,
                alias=self._provider.alias,
            ),
            summary=(
                f"Idle {days}d ({engine}, {nodes} node{'s' if nodes != 1 else ''}, "
                f"no get/set commands) — consider removing"
            ),
            estimated_savings=Money(amount=Decimal(0)),  # unknown: no per-node price
        )
