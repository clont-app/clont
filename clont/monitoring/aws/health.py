"""AWS Health monitoring: open/upcoming account events.

Account-level and global (not per-region). The Health API requires a Business or
Enterprise Support plan; without it the call is denied — we treat that as "no
data" (log + empty) rather than an error.
"""

from __future__ import annotations

from botocore.exceptions import ClientError

from clont.core.logging import get_logger
from clont.core.models import Cloud, CloudResource
from clont.core.registry import register
from clont.monitoring.aws._common import MetricsPolicy
from clont.monitoring.models import HealthCheck, HealthStatus
from clont.providers.aws.parsing import _HealthEvent
from clont.providers.base import Provider

log = get_logger("clont.monitoring.aws.health")

_OPEN = {"eventStatusCodes": ["open", "upcoming"]}


@register("monitoring", Cloud.AWS, "health")
class HealthEventsCollector:
    cloud = Cloud.AWS
    service = "health"

    def __init__(self, provider: Provider, metrics: MetricsPolicy | None = None) -> None:
        self._provider = provider

    def health(self) -> list[HealthCheck]:
        try:
            client = self._provider.client("health", "us-east-1")  # global endpoint
            return self._collect(client)
        except ClientError as exc:  # SubscriptionRequiredException / AccessDenied
            log.info("aws health unavailable for %s: %s", self._provider.alias, exc)
            return []

    def _collect(self, client) -> list[HealthCheck]:
        checks: list[HealthCheck] = []
        for page in client.get_paginator("describe_events").paginate(filter=_OPEN):
            for raw in page.get("events", []):
                ev = _HealthEvent.model_validate(raw)
                checks.append(
                    HealthCheck(
                        resource=CloudResource(
                            cloud=Cloud.AWS,
                            service="health",
                            resource_id=ev.arn or f"{ev.service}:{ev.region}",
                            region=ev.region or None,
                            alias=self._provider.alias,
                        ),
                        status=self._severity(ev),
                        summary=f"{ev.category} {ev.status_code} ({ev.service})",
                    )
                )
        return checks

    @staticmethod
    def _severity(ev: _HealthEvent) -> HealthStatus:
        if ev.category == "issue":
            return HealthStatus.CRITICAL
        return HealthStatus.WARN  # scheduledChange / accountNotification / upcoming
