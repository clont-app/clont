"""Channel protocol + a base that handles severity gating and repeat throttling.
Each channel decides for itself whether to deliver a given event *now*, based
on two knobs:

* `min_severity` — drop events below this level.
* `repeat_after` — how long to wait before re-sending the same condition
  (keyed by `event.key`). `None` means "send once, never repeat" (used by
  notification channels). A timedelta means "send, then repeat at most that
  often" (used by the log so a standing condition isn't logged every cycle).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from clont.events.models import Event, EventSeverity, severity_rank


@runtime_checkable
class Channel(Protocol):
    name: str

    def send(self, event: Event) -> bool: ...


class AbstractChannel(ABC):
    """Base channel: applies severity gating + repeat throttling, then delivers."""

    name: str = "channel"

    def __init__(
        self,
        min_severity: EventSeverity = EventSeverity.WARN,
        repeat_after: timedelta | None = None,
    ) -> None:
        self.min_severity = min_severity
        self.repeat_after = repeat_after
        # key -> last delivery time. In-memory; TODO persist so a restart
        # doesn't re-alert every open condition. Must stay outside the RO role.
        self._last_sent: dict[str, datetime] = {}

    def send(self, event: Event) -> bool:
        """Deliver the event if it passes the severity gate and isn't throttled.

        Returns True if it was actually delivered.
        """
        if severity_rank(event.severity) < severity_rank(self.min_severity):
            return False
        if not self._should_send(event.key):
            return False
        self._deliver(event)
        self._last_sent[event.key] = datetime.now(UTC)
        return True

    def _should_send(self, key: str) -> bool:
        last = self._last_sent.get(key)
        if last is None:
            return True                       # first time -> always send
        if self.repeat_after is None:
            return False                      # send-once channels never repeat
        return datetime.now(UTC) - last >= self.repeat_after

    @abstractmethod
    def _deliver(self, event: Event) -> None: ...
