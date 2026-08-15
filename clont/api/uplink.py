"""The API uplink: ship a cycle's batch to the clont server, dispatch its reply.

Two-way by necessity: channel tokens never leave the agent box, so the server
can't notify Slack/Discord/Telegram itself — any forecast/recommendation it
computes rides home on the POST response and is dispatched through the agent's
own channels.

The wire contract (the future server builds to this):

    POST {url}
    Authorization: Bearer <api_key>
    Content-Type: application/json
    Body:  {"agent": "clont", "metrics": [...], "costs": [...],
            "recommendations": [...], "health": [...], "events": [...]}
    Reply: {"events": [ {key, severity, domain, cloud, title, message,
                         resource?, payload?, timestamp} ]}   # dispatched locally
"""

from __future__ import annotations

from dataclasses import dataclass, field

from clont.api.serialize import parse_events, to_jsonable
from clont.channels._http import post_json_recv
from clont.events.models import Event
from clont.finops.models import CostRecord, Recommendation
from clont.monitoring.models import HealthCheck, MetricPoint


@dataclass(slots=True)
class Batch:
    """Everything one collect cycle produced, gathered for the server."""

    metrics: list[MetricPoint] = field(default_factory=list)
    costs: list[CostRecord] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)
    health: list[HealthCheck] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    # Collector failures the cycle swallowed. Not shipped to the server; it
    # exists so a summary can say "0 events *because* every collector failed"
    # instead of reporting a cheerful, misleading all-clear.
    errors: list[str] = field(default_factory=list)


class ApiUplink:
    """Ships a `Batch` to the configured server and returns the events it sends back.

    Failure isolation lives at the caller (the runner) — a network/HTTP error
    propagates so the cycle can log it and still dispatch its local events.
    """

    def __init__(self, url: str, api_key: str, *, timeout: float = 10.0) -> None:
        self._url = url
        self._api_key = api_key
        self._timeout = timeout

    def ship(self, batch: Batch) -> list[Event]:
        payload = {
            "agent": "clont",
            "metrics": to_jsonable(batch.metrics),
            "costs": to_jsonable(batch.costs),
            "recommendations": to_jsonable(batch.recommendations),
            "health": to_jsonable(batch.health),
            "events": to_jsonable(batch.events),
        }
        _status, body = post_json_recv(
            self._url,
            payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=self._timeout,
        )
        return parse_events(body.get("events", []))
