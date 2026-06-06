"""The agent loop: collect from RO providers, detect events, dispatch to channels."""

from __future__ import annotations

import time
from datetime import date, timedelta

from clont.channels.base import Channel
from clont.core import registry
from clont.core.logging import get_logger
from clont.core.models import Period
from clont.events.detectors import HealthDetector, RecommendationDetector
from clont.events.models import Event
from clont.providers.base import Provider

log = get_logger("clont.agent")


class Agent:
    """Ties providers, detectors and channels together into a cycle.

    One cycle: for every provider, run each registered collector, feed its
    output through the detectors, and hand every event to every channel — each
    channel decides whether to deliver based on its own severity gate and
    repeat throttle. A failing collector or channel is logged and skipped — it
    must not take down the loop.
    """

    def __init__(
        self,
        providers: list[Provider],
        channels: list[Channel],
        *,
        interval_seconds: int = 300,
        lookback_days: int = 1,
    ) -> None:
        self._providers = providers
        self._channels = channels
        self._interval = interval_seconds
        self._lookback = lookback_days
        self._finops_detectors = [RecommendationDetector()]
        self._monitoring_detectors = [HealthDetector()]

    def _period(self) -> Period:
        today = date.today()
        return Period(start=today - timedelta(days=self._lookback), end=today)

    def collect_events(self) -> list[Event]:
        period = self._period()
        events: list[Event] = []
        for provider in self._providers:
            events.extend(self._finops_events(provider, period))
            events.extend(self._monitoring_events(provider, period))
        return events

    def _finops_events(self, provider: Provider, period: Period) -> list[Event]:
        out: list[Event] = []
        for cls in registry.collectors_for("finops", provider.cloud):
            try:
                recs = cls(provider).recommendations(period)
            except Exception as exc:  # noqa: BLE001 - one collector must not kill the loop
                log.warning("finops collector %s failed: %s", cls.__name__, exc)
                continue
            for detector in self._finops_detectors:
                out.extend(detector.detect(recs))
        return out

    def _monitoring_events(self, provider: Provider, period: Period) -> list[Event]:
        out: list[Event] = []
        for cls in registry.collectors_for("monitoring", provider.cloud):
            try:
                checks = cls(provider).health()
            except Exception as exc:  # noqa: BLE001
                log.warning("monitoring collector %s failed: %s", cls.__name__, exc)
                continue
            for detector in self._monitoring_detectors:
                out.extend(detector.detect(checks))
        return out

    def dispatch(self, events: list[Event]) -> int:
        """Hand every event to every channel

        Returns the number of (event, channel) deliveries that actually went
        out this cycle.
        """
        delivered = 0
        for event in events:
            for channel in self._channels:
                try:
                    if channel.send(event):
                        delivered += 1
                except Exception as exc:  # noqa: BLE001 - one channel must not block others
                    log.warning("channel %s failed: %s", channel.name, exc)
        return delivered

    def run_once(self) -> int:
        """Run a single collect -> detect -> dispatch cycle."""
        events = self.collect_events()
        delivered = self.dispatch(events)
        log.info(
            "cycle complete: %d event(s) evaluated, %d deliver(ies)",
            len(events),
            delivered,
        )
        return delivered

    def run_forever(self) -> None:
        log.info("agent starting: interval=%ds, providers=%d", self._interval, len(self._providers))
        while True:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - keep the daemon alive across cycles
                log.exception("cycle failed: %s", exc)
            time.sleep(self._interval)
