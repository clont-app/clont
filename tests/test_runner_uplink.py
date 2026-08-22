"""Agent cycle wiring: ship the batch, dispatch the server's reply, never let
an uplink failure drop the locally-detected events, and keep swallowed
collector failures visible on the batch."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from clont.agent import runner as runner_mod
from clont.agent.runner import Agent
from clont.api.uplink import Batch
from clont.core.models import Cloud, CloudResource, Money
from clont.events.models import Event, EventSeverity
from clont.finops.models import Recommendation


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
    monkeypatch.setattr(agent, "_collect_batch", lambda force=False: Batch(events=[local]))

    delivered = agent.run_once()

    assert len(uplink.shipped) == 1                      # batch was shipped once
    assert {e.key for e in channel.received} == {"local:1", "server:1"}
    assert delivered == 2


def test_no_uplink_ships_nothing(monkeypatch):
    local = _event("local:1")
    channel = _RecordingChannel()
    agent = Agent([], [channel])                          # uplink defaults to None
    monkeypatch.setattr(agent, "_collect_batch", lambda force=False: Batch(events=[local]))

    agent.run_once()

    assert [e.key for e in channel.received] == ["local:1"]


def test_uplink_failure_still_dispatches_local_events(monkeypatch):
    local = _event("local:1")
    channel = _RecordingChannel()
    uplink = _FakeUplink(raises=True)
    agent = Agent([], [channel], uplink=uplink)
    monkeypatch.setattr(agent, "_collect_batch", lambda force=False: Batch(events=[local]))

    delivered = agent.run_once()                          # must not raise

    assert [e.key for e in channel.received] == ["local:1"]
    assert delivered == 1

# --- cycle seam: the batch a summary is built from --------------------------


def test_run_cycle_returns_batch_carrying_server_events(monkeypatch):
    local = _event("local:1")
    server = _event("server:1", title="from server")
    agent = Agent([], [_RecordingChannel()], uplink=_FakeUplink(returns=[server]))
    monkeypatch.setattr(agent, "_collect_batch", lambda force=False: Batch(events=[local]))

    batch, delivered = agent.run_cycle()

    # A summary built from the batch must see exactly what was dispatched.
    assert {e.key for e in batch.events} == {"local:1", "server:1"}
    assert delivered == 2


def test_collector_failures_are_recorded_on_the_batch(monkeypatch):
    class _Boom:
        def __init__(self, *args) -> None: ...

        def collect(self, period):
            raise RuntimeError("AccessDenied")

        def recommendations(self, period):
            raise RuntimeError("AccessDenied")

    monkeypatch.setattr(
        runner_mod.registry,
        "collectors_for",
        lambda kind, cloud: [_Boom] if kind == "finops" else [],
    )
    agent = Agent([SimpleNamespace(cloud=Cloud.AWS, alias="prod")], [])

    batch = agent._collect_batch()

    # Swallowed so the loop survives, but not silent: a "0 events" summary
    # would otherwise read as an all-clear.
    assert batch.events == []
    assert len(batch.errors) == 2
    assert all("AccessDenied" in e and "_Boom" in e for e in batch.errors)


def test_the_same_resource_is_only_recommended_once(monkeypatch):
    # Compute Optimizer and the opted-in CloudWatch fallback both call i-1 idle;
    # the operator should hear it once.
    def _rec(savings: str):
        return Recommendation(
            cloud=str(Cloud.AWS),
            service="ec2",
            kind="idle",
            resource=CloudResource(
                cloud=Cloud.AWS, service="ec2", resource_id="i-1", alias="prod"
            ),
            summary=f"idle ({savings})",
            estimated_savings=Money(amount=Decimal(savings)),
        )

    class _Source:
        def __init__(self, savings: str) -> None:
            self._savings = savings

        def collect(self, period):
            return []

        def recommendations(self, period):
            return [_rec(self._savings)]

    monkeypatch.setattr(
        runner_mod.registry,
        "collectors_for",
        lambda kind, cloud: (
            [lambda *a: _Source("61"), lambda *a: _Source("0")]
            if kind == "finops"
            else []
        ),
    )
    agent = Agent([SimpleNamespace(cloud=Cloud.AWS, alias="prod")], [])

    batch = agent._collect_batch()

    assert len(batch.recommendations) == 1
    assert batch.recommendations[0].estimated_savings.amount == Decimal("61")
    assert len(batch.events) == 1
