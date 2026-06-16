"""API serialization: to_jsonable (models -> JSON-safe) + parse_events (reply -> Event)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

from clont.api.serialize import parse_events, to_jsonable
from clont.core.models import Cloud, CloudResource, Money, Period
from clont.events.models import Event, EventSeverity
from clont.finops.models import CostRecord, Recommendation
from clont.monitoring.models import HealthCheck, HealthStatus, MetricPoint

_RESOURCE = CloudResource(
    cloud=Cloud.AWS, service="rds", resource_id="db-1", region="us-east-1", alias="prod"
)


def test_scalars_convert():
    assert to_jsonable(Cloud.AWS) == "aws"                       # StrEnum -> value
    assert to_jsonable(Decimal("12.34")) == "12.34"              # Decimal -> str
    assert to_jsonable(datetime(2024, 1, 2, 3, 4, tzinfo=UTC)) == "2024-01-02T03:04:00+00:00"
    assert to_jsonable(date(2024, 1, 2)) == "2024-01-02"
    assert to_jsonable(EventSeverity.WARN) == "warn"


def test_nested_value_types():
    assert to_jsonable(Money(Decimal("9.99"))) == {"amount": "9.99", "currency": "USD"}
    assert to_jsonable(Period(date(2024, 1, 1), date(2024, 1, 31))) == {
        "start": "2024-01-01",
        "end": "2024-01-31",
    }
    assert to_jsonable(_RESOURCE) == {
        "cloud": "aws",
        "service": "rds",
        "resource_id": "db-1",
        "region": "us-east-1",
        "alias": "prod",
    }


def test_metric_point_is_json_dumpable():
    point = MetricPoint(
        name="FreeStoragePercent",
        value=42.5,
        unit="Percent",
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        resource=_RESOURCE,
    )
    out = to_jsonable(point)
    # round-trips through the stdlib encoder (the real uplink constraint)
    assert json.loads(json.dumps(out))["name"] == "FreeStoragePercent"
    assert out["resource"]["resource_id"] == "db-1"


def test_cost_record_preserves_decimal_precision():
    record = CostRecord(
        cloud="aws",
        service="ec2",
        period=Period(date(2024, 1, 1), date(2024, 1, 2)),
        cost=Money(Decimal("123.456789")),
        alias="prod",
        dimensions={"usage_type": "BoxUsage"},
    )
    out = to_jsonable(record)
    assert out["cost"]["amount"] == "123.456789"
    assert out["resource"] is None
    assert out["dimensions"] == {"usage_type": "BoxUsage"}


def test_recommendation_and_health_dumpable():
    rec = Recommendation(
        cloud="aws", service="ec2", resource=_RESOURCE,
        summary="idle", estimated_savings=Money(Decimal("0")),
    )
    health = HealthCheck(resource=_RESOURCE, status=HealthStatus.WARN, summary="degraded")
    assert json.dumps(to_jsonable(rec))            # no raise
    assert to_jsonable(health)["status"] == "warn"


def test_event_round_trips_through_to_jsonable_and_parse():
    event = Event(
        key="monitoring:rule:prod:aws:rds:db-1:FreeStoragePercent",
        severity=EventSeverity.WARN,
        domain="monitoring",
        cloud=Cloud.AWS,
        title="Low free storage",
        message="below 10%",
        resource=_RESOURCE,
        payload={"value": 8.0},
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
    )
    wire = json.loads(json.dumps(to_jsonable(event)))
    [back] = parse_events([wire])
    assert back.key == event.key
    assert back.severity is EventSeverity.WARN
    assert back.cloud is Cloud.AWS
    assert back.resource == _RESOURCE
    assert back.payload == {"value": 8.0}
    assert back.timestamp == event.timestamp


def test_parse_events_skips_malformed_items():
    good = {
        "key": "k", "severity": "warn", "domain": "finops", "cloud": "aws",
        "title": "t", "message": "m",
    }
    bad_severity = {**good, "severity": "nope"}
    missing_field = {"key": "k", "severity": "warn"}
    events = parse_events([good, bad_severity, missing_field, "not-a-dict"])
    assert len(events) == 1
    assert events[0].resource is None          # optional fields default cleanly


def test_parse_events_non_list_is_empty():
    assert parse_events(None) == []
    assert parse_events({"events": []}) == []


def test_to_jsonable_raises_on_unhandled_type():
    import pytest

    # An unexpected type (here a set) must fail loudly at serialization, not
    # silently pass through to blow up later inside json.dumps.
    with pytest.raises(TypeError, match="set"):
        to_jsonable({"tags": {"a", "b"}})
