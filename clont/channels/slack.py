"""SlackChannel: post events to a Slack Incoming Webhook."""

from __future__ import annotations

from datetime import timedelta

from clont.channels._http import post_json
from clont.channels.base import AbstractChannel
from clont.events.models import Event, EventSeverity

_EMOJI = {
    EventSeverity.INFO: ":information_source:",
    EventSeverity.WARN: ":warning:",
    EventSeverity.CRITICAL: ":rotating_light:",
}


class SlackChannel(AbstractChannel):
    name = "slack"

    def __init__(
        self,
        webhook_url: str,
        min_severity: EventSeverity = EventSeverity.WARN,
        repeat_after: timedelta | None = None,
    ) -> None:
        super().__init__(min_severity=min_severity, repeat_after=repeat_after)
        self._webhook_url = webhook_url

    def _deliver(self, event: Event) -> None:
        emoji = _EMOJI.get(event.severity, "")
        text = f"{emoji} *{event.title}*\n{event.message}"
        post_json(self._webhook_url, {"text": text})
