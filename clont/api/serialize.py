"""JSON (de)serialization for the API uplink.

The domain models are plain frozen dataclasses holding `Decimal`/`datetime`/
`date`/`StrEnum` values that the stdlib `json` encoder can't handle, and they
carry no serializers of their own. `to_jsonable` turns any of them (and nested
structures) into JSON-safe primitives; `parse_events` rebuilds the `Event`s the
server returns so they can flow into the normal dispatch loop.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from clont.core.logging import get_logger
from clont.core.models import Cloud, CloudResource
from clont.events.models import Event, EventSeverity

log = get_logger("clont.api")


def to_jsonable(obj: Any) -> Any:
    """Recursively convert `obj` into JSON-serializable primitives.

    Dataclasses become dicts; `datetime`/`date` ISO strings; `Decimal` strings
    (to preserve money precision); enums their value. Containers are recursed.
    """
    # Enum must precede str/int: StrEnum/IntEnum members are also str/int.
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [to_jsonable(v) for v in obj]
    if obj is None or isinstance(obj, str | int | float | bool):
        return obj
    # Fail loudly here rather than letting json.dumps blow up later with a vague
    # error far from the offending field.
    raise TypeError(f"to_jsonable cannot serialize {type(obj).__name__}")


def _parse_resource(data: Any) -> CloudResource | None:
    if not isinstance(data, dict):
        return None
    return CloudResource(
        cloud=Cloud(data["cloud"]),
        service=data["service"],
        resource_id=data["resource_id"],
        region=data.get("region"),
        alias=data.get("alias"),
    )


def parse_events(items: Any) -> list[Event]:
    """Rebuild `Event`s from the server's JSON reply.

    Defensive by design: a malformed item is logged and skipped rather than
    allowed to crash local dispatch — a bad server response must never silence
    the agent's own events.
    """
    if not isinstance(items, list):
        return []
    events: list[Event] = []
    for item in items:
        try:
            events.append(
                Event(
                    key=item["key"],
                    severity=EventSeverity(item["severity"]),
                    domain=item["domain"],
                    cloud=Cloud(item["cloud"]),
                    title=item["title"],
                    message=item["message"],
                    resource=_parse_resource(item.get("resource")),
                    payload=item.get("payload") or {},
                    timestamp=datetime.fromisoformat(item["timestamp"])
                    if item.get("timestamp")
                    else datetime.now(UTC),
                )
            )
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("skipping malformed server event: %s", exc)
    return events
