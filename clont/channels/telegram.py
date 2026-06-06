"""TelegramChannel: send events via a Telegram bot.

Setup: create a bot with @BotFather to get a token, and obtain the target
`chat_id` (a user, group, or channel). Delivery is a plain HTTP POST to the
Bot API — no SDK needed.
"""

from __future__ import annotations

import html
from datetime import timedelta

from clont.channels._http import post_json
from clont.channels.base import AbstractChannel
from clont.events.models import Event, EventSeverity

_EMOJI = {
    EventSeverity.INFO: "\N{INFORMATION SOURCE}",
    EventSeverity.WARN: "\N{WARNING SIGN}",
    EventSeverity.CRITICAL: "\N{POLICE CARS REVOLVING LIGHT}",
}


class TelegramChannel(AbstractChannel):
    name = "telegram"

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        min_severity: EventSeverity = EventSeverity.WARN,
        repeat_after: timedelta | None = None,
    ) -> None:
        super().__init__(min_severity=min_severity, repeat_after=repeat_after)
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id

    def _deliver(self, event: Event) -> None:
        emoji = _EMOJI.get(event.severity, "")
        # HTML parse mode: escape dynamic text, bold the title.
        text = f"{emoji} <b>{html.escape(event.title)}</b>\n{html.escape(event.message)}"
        post_json(
            self._url,
            {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"},
        )
