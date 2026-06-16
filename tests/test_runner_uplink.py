"""Agent.run_once uplink wiring: ship the batch, dispatch the server's reply,
and never let an uplink failure drop the locally-detected events."""

from __future__ import annotations

from clont.agent.runner import Agent
from clont.api.uplink import Batch
from clont.core.models import Cloud
from clont.events.models import Event, EventSeverity


class _RecordingChannel:
    name = "recording"

    def __init__(self) -> None:
        self.received: list[Event] = []

    def send(self, event: Event) -> bool:
        self.received.append(event)
        return True


class _FakeUplink:
    def __init__(self, returns=None, raises=False) -> None:
        self._returns = returns or []
        self._raises = raises
        self.shipped: list[Batch] = []

    def ship(self, batch: Batch) -> list[Event]:
        self.shipped.append(batch)
        if self._raises:
            raise RuntimeError("server unreachable")
        return self._returns


def _event(key: str, *, title="t") -> Event:
    return Event(key, EventSeverity.WARN, "monitoring", Cloud.AWS, title, "m")


def test_run_once_ships_batch_and_dispatches_server_events(monkeypatch):
    local = _event("local:1")
    server = _event("server:1", title="from server")
    channel = _RecordingChannel()
    uplink = _FakeUplink(returns=[server])
    agent = Agent([], [channel], uplink=uplink)
    monkeypatch.setattr(agent, "_collect_batch", lambda: Batch(events=[local]))

    delivered = agent.run_once()

    assert len(uplink.shipped) == 1                      # batch was shipped once
    assert {e.key for e in channel.received} == {"local:1", "server:1"}
    assert delivered == 2


def test_no_uplink_ships_nothing(monkeypatch):
    local = _event("local:1")
    channel = _RecordingChannel()
    agent = Agent([], [channel])                          # uplink defaults to None
    monkeypatch.setattr(agent, "_collect_batch", lambda: Batch(events=[local]))

    agent.run_once()

    assert [e.key for e in channel.received] == ["local:1"]


def test_uplink_failure_still_dispatches_local_events(monkeypatch):
    local = _event("local:1")
    channel = _RecordingChannel()
    uplink = _FakeUplink(raises=True)
    agent = Agent([], [channel], uplink=uplink)
    monkeypatch.setattr(agent, "_collect_batch", lambda: Batch(events=[local]))

    delivered = agent.run_once()                          # must not raise

    assert [e.key for e in channel.received] == ["local:1"]
    assert delivered == 1
