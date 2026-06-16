"""Idle RDS detection: low connections + low CPU over the window."""

from __future__ import annotations

from decimal import Decimal

from clont.events.detectors import RecommendationDetector
from clont.finops.aws.idle_rds import IdleRDSCollector


class _Paginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **kw):
        yield from self._pages


class _FakeRDS:
    def __init__(self, instances: list[dict]) -> None:
        self._instances = instances

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "describe_db_instances"
        return _Paginator([{"DBInstances": self._instances}])


class _FakeCW:
    def __init__(self, by_db: dict[str, dict[str, list[float]]]) -> None:
        self._by = by_db

    def get_metric_data(self, MetricDataQueries, **kw) -> dict:
        results = []
        for q in MetricDataQueries:
            metric = q["MetricStat"]["Metric"]["MetricName"]
            db_id = q["MetricStat"]["Metric"]["Dimensions"][0]["Value"]
            results.append({"Id": q["Id"], "Values": self._by.get(db_id, {}).get(metric, [])})
        return {"MetricDataResults": results}


class _FakeProvider:
    def __init__(self, rds: _FakeRDS, cw: _FakeCW, alias: str = "prod") -> None:
        self._clients = {"rds": rds, "cloudwatch": cw}
        self.alias = alias

    def regions(self) -> list[str]:
        return ["us-east-1"]

    def client(self, service: str, region: str | None = None):
        return self._clients[service]


def _db(db_id: str, status: str = "available") -> dict:
    return {"DBInstanceIdentifier": db_id, "DBInstanceStatus": status}


def _metrics(connections, cpu):
    return {"DatabaseConnections": connections, "CPUUtilization": cpu}


def test_only_low_connection_low_cpu_dbs_are_flagged():
    cw = _FakeCW({
        "db-idle": _metrics([0.0, 0.0], cpu=[1.0]),            # idle
        "db-busy-conns": _metrics([10.0], cpu=[1.0]),          # has connections
        "db-busy-cpu": _metrics([0.0], cpu=[40.0]),            # CPU busy (background work)
        "db-nodata": {"DatabaseConnections": [], "CPUUtilization": []},  # no metrics
    })
    rds = _FakeRDS([
        _db("db-idle"), _db("db-busy-conns"), _db("db-busy-cpu"), _db("db-nodata"),
    ])

    recs = IdleRDSCollector(_FakeProvider(rds, cw)).recommendations(None)

    assert [r.resource.resource_id for r in recs] == ["db-idle"]
    [rec] = recs
    assert rec.service == "rds"
    assert rec.kind == "idle"
    assert rec.estimated_savings.amount == Decimal(0)  # unknown
    assert "Idle" in rec.summary


def test_stopped_dbs_are_skipped():
    cw = _FakeCW({"db-idle": _metrics([0.0], cpu=[0.5])})
    rds = _FakeRDS([_db("db-idle", status="stopped")])
    recs = IdleRDSCollector(_FakeProvider(rds, cw)).recommendations(None)
    assert recs == []


def test_idle_rds_event_key_and_no_savings_clause():
    cw = _FakeCW({"db-idle": _metrics([0.0], cpu=[0.5])})
    recs = IdleRDSCollector(_FakeProvider(_FakeRDS([_db("db-idle")]), cw)).recommendations(None)
    [event] = RecommendationDetector().detect(recs)
    assert event.key == "finops:rec:prod:aws:rds:idle:db-idle"
    assert "est. savings" not in event.message


def test_no_dbs_no_recs():
    recs = IdleRDSCollector(_FakeProvider(_FakeRDS([]), _FakeCW({}))).recommendations(None)
    assert recs == []
