"""LogChannel: structured logging of events.

Logs every *new* condition immediately, then re-logs a standing condition at
most once per `repeat_after` (so an unresolved issue doesn't spam the log every
cycle).
"""

from __future__ import annotations

import json
from datetime import timedelta

from clont.channels.base import AbstractChannel
from clont.core.logging import get_logger
from clont.events.models import Event, EventSeverity

log = get_logger("clont.events")


class LogChannel(AbstractChannel):
    name = "log"

    def __init__(
        self,
        repeat_after: timedelta = timedelta(hours=3),
        min_severity: EventSeverity = EventSeverity.INFO,
    ) -> None:
        super().__init__(min_severity=min_severity, repeat_after=repeat_after)

    def _deliver(self, event: Event) -> None:
        record = {
            "key": event.key,
            "severity": event.severity,
            "domain": event.domain,
            "cloud": event.cloud,
            "title": event.title,
            "message": event.message,
            "resource": event.resource.resource_id if event.resource else None,
            "timestamp": event.timestamp.isoformat(),
            **event.payload,
        }
        log.info(json.dumps(record))
