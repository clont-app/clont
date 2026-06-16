"""Capacity/pressure metric collectors use worst-case CloudWatch statistics
(not Average, which smooths the very spike/dip the detectors exist to catch), and
RDS skips the free-storage forecast for storage-autoscaling instances.
"""

from __future__ import annotations

from datetime import UTC, datetime

from clont.core.models import Period
from clont.monitoring.aws.elasticache import ElastiCacheHealthCollector
from clont.monitoring.aws.rds import RDSHealthCollector
from clont.monitoring.aws.redshift import RedshiftHealthCollector

PERIOD = Period(datetime(2024, 1, 1).date(), datetime(2024, 1, 1).date())
_T = datetime(2024, 1, 1, 10, tzinfo=UTC)


class _Paginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **kw):
        yield from self._pages


class _Lister:
    """A read-only client that paginates one list response."""

    def __init__(self, paginator_name: str, page: dict) -> None:
        self._name = paginator_name
        self._page = page

    def get_paginator(self, name: str) -> _Paginator:
        assert name == self._name
        return _Paginator([self._page])


class _CW:
    """Records the queries it was asked for and echoes one datapoint each."""

    def __init__(self) -> None:
        self.queries: list[dict] = []

    def get_metric_data(self, **kw):
        self.queries = kw["MetricDataQueries"]
        return {
            "MetricDataResults": [
                {"Id": q["Id"], "Timestamps": [_T], "Values": [50.0]} for q in self.queries
            ]
        }


class _Provider:
    def __init__(self, clients: dict) -> None:
        self._clients = clients
        self.alias = "prod"

    def regions(self) -> list[str]:
        return ["us-east-1"]

    def client(self, service: str, region: str | None = None):
        return self._clients[service]


def _stats(cw: _CW) -> set[str]:
    return {q["MetricStat"]["Stat"] for q in cw.queries}


def test_rds_uses_minimum_and_skips_autoscaling_free_storage():
    rds = _Lister(
        "describe_db_instances",
        {
            "DBInstances": [
                {"DBInstanceIdentifier": "db-normal", "DBInstanceStatus": "available",
                 "AllocatedStorage": 100},
                {"DBInstanceIdentifier": "db-auto", "DBInstanceStatus": "available",
                 "AllocatedStorage": 100, "MaxAllocatedStorage": 1000},
            ]
        },
    )
    cw = _CW()
    points = RDSHealthCollector(_Provider({"rds": rds, "cloudwatch": cw})).collect(PERIOD)

    assert _stats(cw) == {"Minimum"}  # worst-case low point, not Average
    free = {p.resource.resource_id for p in points if p.name == "FreeStoragePercent"}
    credit = {p.resource.resource_id for p in points if p.name == "CPUCreditBalance"}
    assert free == {"db-normal"}                       # autoscaling db's forecast suppressed
    assert credit == {"db-normal", "db-auto"}          # credit signal still collected for both


def test_redshift_uses_maximum():
    rs = _Lister("describe_clusters", {"Clusters": [{"ClusterIdentifier": "rs-1"}]})
    cw = _CW()
    points = RedshiftHealthCollector(_Provider({"redshift": rs, "cloudwatch": cw})).collect(PERIOD)
    assert _stats(cw) == {"Maximum"}                   # peak disk-used, not Average
    assert all(p.name == "PercentageDiskSpaceUsed" for p in points)


def test_elasticache_uses_max_swap_and_min_freeable():
    ec = _Lister(
        "describe_cache_clusters",
        {"CacheClusters": [{"CacheClusterId": "cc-1", "CacheClusterStatus": "available"}]},
    )
    cw = _CW()
    ElastiCacheHealthCollector(_Provider({"elasticache": ec, "cloudwatch": cw})).collect(PERIOD)
    by_metric = {q["MetricStat"]["Metric"]["MetricName"]: q["MetricStat"]["Stat"] for q in cw.queries}
    assert by_metric == {"SwapUsage": "Maximum", "FreeableMemory": "Minimum"}
