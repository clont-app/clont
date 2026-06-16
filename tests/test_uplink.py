"""ApiUplink: payload envelope + Bearer auth + parsing the server's event reply."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import clont.api.uplink as uplink_mod
from clont.api.uplink import ApiUplink, Batch
from clont.core.models import Cloud, CloudResource, Money, Period
from clont.events.models import Event, EventSeverity
from clont.finops.models import CostRecord, Recommendation
from clont.monitoring.models import HealthCheck, HealthStatus, MetricPoint

_RESOURCE = CloudResource(cloud=Cloud.AWS, service="rds", resource_id="db-1", alias="prod")


def _full_batch() -> Batch:
    return Batch(
        metrics=[MetricPoint("FreeStoragePercent", 8.0, "Percent",
                             datetime(2024, 1, 1, tzinfo=UTC), _RESOURCE)],
        costs=[CostRecord("aws", "ec2", Period(date(2024, 1, 1), date(2024, 1, 2)),
                          Money(Decimal("12.34")), alias="prod")],
        recommendations=[Recommendation("aws", "ec2", _RESOURCE, "idle", Money(Decimal("0")))],
        health=[HealthCheck(_RESOURCE, HealthStatus.WARN, "degraded")],
        events=[Event("k", EventSeverity.WARN, "monitoring", Cloud.AWS, "t", "m")],
    )


def _capture(monkeypatch, reply: dict):
    calls: dict = {}

    def fake_post(url, payload, *, headers=None, timeout=10.0):
        calls.update(url=url, payload=payload, headers=headers, timeout=timeout)
        return 200, reply

    monkeypatch.setattr(uplink_mod, "post_json_recv", fake_post)
    return calls


def test_ship_sends_bearer_auth_and_full_envelope(monkeypatch):
    calls = _capture(monkeypatch, {"events": []})
    ApiUplink("https://api.example/ingest", "secret-key", timeout=5.0).ship(_full_batch())

    assert calls["url"] == "https://api.example/ingest"
    assert calls["headers"] == {"Authorization": "Bearer secret-key"}
    assert calls["timeout"] == 5.0
    payload = calls["payload"]
    assert payload["agent"] == "clont"
    assert set(payload) == {"agent", "metrics", "costs", "recommendations", "health", "events"}
    # each section serialized to JSON-safe primitives
    assert payload["metrics"][0]["name"] == "FreeStoragePercent"
    assert payload["costs"][0]["cost"]["amount"] == "12.34"
    assert payload["events"][0]["severity"] == "warn"


def test_ship_returns_parsed_server_events(monkeypatch):
    reply = {
        "events": [{
            "key": "server:forecast:prod:aws:rds:db-1",
            "severity": "critical",
            "domain": "monitoring",
            "cloud": "aws",
            "title": "Predicted full in 3d",
            "message": "server forecast",
        }]
    }
    _capture(monkeypatch, reply)
    [event] = ApiUplink("u", "k").ship(_full_batch())
    assert isinstance(event, Event)
    assert event.severity is EventSeverity.CRITICAL
    assert event.title == "Predicted full in 3d"


def test_ship_tolerates_missing_events_key(monkeypatch):
    _capture(monkeypatch, {})
    assert ApiUplink("u", "k").ship(Batch()) == []
