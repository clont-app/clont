"""idle elasticache and dynamodb: no commands / no consumed capacity over the window"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from clont.events.detectors import RecommendationDetector
from clont.finops.aws.idle_dynamodb import IdleDynamoDBCollector
from clont.finops.aws.idle_elasticache import IdleElastiCacheCollector
from clont.finops.base import FinOpsTuning

_TS = datetime(2026, 1, 1, tzinfo=UTC)
_ON = FinOpsTuning(allow_cloudwatch_metrics=True)


class _Paginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **kw):
        yield from self._pages


class _FakeCW:
    """values keyed by (metric, dims as a sorted tuple); a missing key = no datapoints"""

    def __init__(self, data: dict) -> None:
        self.calls = 0
        self._data = data

    def get_metric_data(self, MetricDataQueries, **kw) -> dict:
        self.calls += 1
        results = []
        for q in MetricDataQueries:
            m = q["MetricStat"]["Metric"]
            dims = tuple(sorted((d["Name"], d["Value"]) for d in m["Dimensions"]))
            values = self._data.get((m["MetricName"], dims), [])
            results.append({"Id": q["Id"], "Timestamps": [_TS] * len(values), "Values": values})
        return {"MetricDataResults": results}


class _FakeProvider:
    def __init__(self, clients: dict, alias: str = "prod") -> None:
        self._clients = clients
        self.alias = alias

    def regions(self) -> list[str]:
        return ["us-east-1"]

    def client(self, service: str, region: str | None = None):
        return self._clients[service]


class _FakeEC:
    def __init__(self, clusters: list[dict]) -> None:
        self._clusters = clusters

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "describe_cache_clusters"
        return _Paginator([{"CacheClusters": self._clusters}])


def _cache(cid, engine="redis", group=None, status="available") -> dict:
    c = {"CacheClusterId": cid, "Engine": engine, "CacheClusterStatus": status}
    if group:
        c["ReplicationGroupId"] = group
    return c


def _ec_cmds(cid, gets, sets, engine="redis"):
    get, set_ = ("CmdGet", "CmdSet") if engine == "memcached" else ("GetTypeCmds", "SetTypeCmds")
    key = (("CacheClusterId", cid),)
    return {(get, key): gets, (set_, key): sets}


def _ec_recs(clusters, data, tuning=_ON):
    cw = _FakeCW(data)
    provider = _FakeProvider({"elasticache": _FakeEC(clusters), "cloudwatch": cw})
    return IdleElastiCacheCollector(provider, tuning).recommendations(None), cw


def test_cache_with_no_commands_is_idle_and_busy_one_is_not():
    data = {
        **_ec_cmds("idle-1", [0.0, 0.0], [0.0]),
        **_ec_cmds("busy-1", [500.0], [0.0]),
        **_ec_cmds("mc-idle", [0.0], [0.0], engine="memcached"),
    }
    recs, _ = _ec_recs(
        [_cache("idle-1"), _cache("busy-1"), _cache("mc-idle", engine="memcached")], data
    )
    assert sorted(r.resource.resource_id for r in recs) == ["idle-1", "mc-idle"]
    rec = next(r for r in recs if r.resource.resource_id == "idle-1")
    assert rec.service == "elasticache"
    assert rec.kind == "idle"
    assert rec.estimated_savings.amount == Decimal(0)
    assert "no get/set" in rec.summary


def test_replication_group_is_judged_as_one():
    # writes land on the primary only: the group is in use, the replica alone is not idle
    data = {
        **_ec_cmds("rg-001", [0.0], [10.0]),
        **_ec_cmds("rg-002", [0.0], [0.0]),
        **_ec_cmds("dead-001", [0.0], [0.0]),
        **_ec_cmds("dead-002", [0.0], [0.0]),
    }
    clusters = [
        _cache("rg-001", group="rg"), _cache("rg-002", group="rg"),
        _cache("dead-001", group="dead"), _cache("dead-002", group="dead"),
    ]
    recs, _ = _ec_recs(clusters, data)
    assert [r.resource.resource_id for r in recs] == ["dead"]
    assert "2 nodes" in recs[0].summary


def test_cache_without_datapoints_is_not_called_idle():
    recs, _ = _ec_recs([_cache("nodata")], {})
    assert recs == []


def test_cache_not_available_is_skipped():
    recs, cw = _ec_recs([_cache("c", status="modifying")], _ec_cmds("c", [0.0], [0.0]))
    assert recs == []
    assert cw.calls == 0


def test_elasticache_event_key():
    recs, _ = _ec_recs([_cache("idle-1")], _ec_cmds("idle-1", [0.0], [0.0]))
    [event] = RecommendationDetector().detect(recs)
    assert event.key == "finops:rec:prod:aws:elasticache:idle:idle-1"


def test_elasticache_needs_the_opt_in():
    recs, cw = _ec_recs([_cache("idle-1")], _ec_cmds("idle-1", [0.0], [0.0]), FinOpsTuning())
    assert recs == []
    assert cw.calls == 0


class _FakeDDB:
    def __init__(self, tables: dict[str, dict]) -> None:
        self._tables = tables

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "list_tables"
        return _Paginator([{"TableNames": list(self._tables)}])

    def describe_table(self, TableName: str) -> dict:
        return {"Table": self._tables[TableName]}


_OLD = datetime.now(UTC) - timedelta(days=60)


def _table(name, mode="PAY_PER_REQUEST", created=_OLD, status="ACTIVE", gsis=()) -> dict:
    t = {"TableName": name, "TableStatus": status, "CreationDateTime": created}
    if mode:
        t["BillingModeSummary"] = {"BillingMode": mode}
    if gsis:
        t["GlobalSecondaryIndexes"] = [{"IndexName": g} for g in gsis]
    return t


def _ddb_recs(tables, data, tuning=_ON):
    cw = _FakeCW(data)
    ddb = _FakeDDB({t["TableName"]: t for t in tables})
    provider = _FakeProvider({"dynamodb": ddb, "cloudwatch": cw})
    return IdleDynamoDBCollector(provider, tuning).recommendations(None), cw


def _rcu(table, values, gsi=None):
    dims = [("TableName", table)] + ([("GlobalSecondaryIndexName", gsi)] if gsi else [])
    return {("ConsumedReadCapacityUnits", tuple(sorted(dims))): values}


def _wcu(table, values):
    return {("ConsumedWriteCapacityUnits", (("TableName", table),)): values}


def test_table_with_no_reads_or_writes_is_idle():
    data = {**_rcu("busy", [3.0]), **_wcu("written", [1.0])}
    tables = [_table("quiet"), _table("busy"), _table("written")]
    recs, _ = _ddb_recs(tables, data)
    assert [r.resource.resource_id for r in recs] == ["quiet"]
    [rec] = recs
    assert rec.service == "dynamodb"
    assert rec.kind == "idle"
    assert "only storage" in rec.summary


def test_reads_through_a_gsi_keep_the_table_in_use():
    data = _rcu("by-index", [7.0], gsi="gsi1")
    recs, _ = _ddb_recs([_table("by-index", gsis=["gsi1"])], data)
    assert recs == []


def test_young_and_inactive_tables_are_skipped():
    young = _table("new", created=datetime.now(UTC) - timedelta(days=2))
    recs, cw = _ddb_recs([young, _table("gone", status="DELETING")], {})
    assert recs == []
    assert cw.calls == 0


def test_provisioned_table_advice_and_missing_billing_summary():
    recs, _ = _ddb_recs([_table("legacy", mode=None)], {})
    [rec] = recs
    assert "provisioned" in rec.summary


def test_dynamodb_needs_the_opt_in():
    recs, cw = _ddb_recs([_table("quiet")], {}, FinOpsTuning())
    assert recs == []
    assert cw.calls == 0
