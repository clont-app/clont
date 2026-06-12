"""The agent loop: collect from RO providers, detect events, dispatch to channels."""

from __future__ import annotations

import time
from datetime import date, timedelta

from clont.api.uplink import ApiUplink, Batch
from clont.channels.base import Channel
from clont.core import registry
from clont.core.logging import get_logger
from clont.core.config import BudgetRule
from clont.core.models import Period
from clont.events.detectors import (
    BudgetDetector,
    CapacityForecastDetector,
    HealthDetector,
    MetricAnomalyDetector,
    RecommendationDetector,
    SpendDigestDetector,
    SpendForecastDetector,
    SpendSpikeDetector,
    ThresholdRuleDetector,
)
from clont.events.models import Event
from clont.finops.base import FinOpsTuning
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
        spend_baseline_days: int = 28,
        spend_spike_pct: float = 50.0,
        spend_min_dollars: float = 1.0,
        budgets: list[BudgetRule] | None = None,
        budget_warn_pct: float = 80.0,
        forecast_alpha: float = 0.5,
        finops_tuning: FinOpsTuning | None = None,
        anomaly_sigma: float = 3.0,
        anomaly_min_points: int = 6,
        free_storage_min_pct: float = 10.0,
        disk_used_max_pct: float = 90.0,
        cpu_credit_min_balance: float = 20.0,
        swap_usage_max_mb: float = 50.0,
        disk_full_forecast_days: float = 14.0,
        uplink: ApiUplink | None = None,
    ) -> None:
        self._providers = providers
        self._channels = channels
        self._uplink = uplink
        self._interval = interval_seconds
        self._lookback = lookback_days
        self._spend_baseline_days = spend_baseline_days
        self._finops_tuning = finops_tuning or FinOpsTuning()
        self._finops_detectors = [RecommendationDetector()]
        self._finops_cost_detectors = [
            SpendDigestDetector(),
            SpendSpikeDetector(spend_spike_pct, spend_min_dollars),
            SpendForecastDetector(forecast_alpha),
            BudgetDetector(budgets or [], budget_warn_pct, forecast_alpha),
        ]
        self._monitoring_detectors = [HealthDetector()]
        self._monitoring_metric_detectors = [
            MetricAnomalyDetector(anomaly_sigma, anomaly_min_points),
            ThresholdRuleDetector(
                free_storage_min_pct,
                disk_used_max_pct,
                cpu_credit_min_balance,
                swap_usage_max_mb,
            ),
            CapacityForecastDetector(disk_full_forecast_days),
        ]

    def _period(self) -> Period:
        today = date.today()
        return Period(start=today - timedelta(days=self._lookback), end=today)

    def _cost_period(self) -> Period:
        # End at *yesterday*, the last complete day: today's Cost Explorer data
        # is partial/estimated and would under-report the digest and mask spikes.
        # Window is `spend_baseline_days` prior days + that anchor day, but always
        # reaches back to the 1st so the same single pull covers the full
        # month-to-date the forecast + budget detectors need (late in the month
        # the trailing window alone would miss the first days).
        end = date.today() - timedelta(days=1)
        start = min(end - timedelta(days=self._spend_baseline_days), end.replace(day=1))
        return Period(start=start, end=end)

    def collect_events(self) -> list[Event]:
        return self._collect_batch().events

    def _collect_batch(self) -> Batch:
        """Run every collector once per provider, gathering the cycle's raw data
        and the events its detectors fire into a single `Batch`.

        The raw data (metrics/costs/recommendations/health) is what the API
        uplink ships.
        """
        period = self._period()
        batch = Batch()
        for provider in self._providers:
            self._finops_collect(provider, period, batch)
            self._monitoring_collect(provider, period, batch)
        return batch

    def _finops_collect(self, provider: Provider, period: Period, batch: Batch) -> None:
        cost_period = self._cost_period()
        for cls in registry.collectors_for("finops", provider.cloud):
            collector = cls(provider, self._finops_tuning)
            # Spend (cost records -> digest + spike) and recommendations are
            # collected independently so one failing can't drop the other.
            try:
                records = collector.collect(cost_period)
            except Exception as exc:  # noqa: BLE001 - one collector must not kill the loop
                log.warning("finops collector %s collect failed: %s", cls.__name__, exc)
            else:
                batch.costs.extend(records)
                for detector in self._finops_cost_detectors:
                    batch.events.extend(detector.detect(records))
            try:
                recs = collector.recommendations(period)
            except Exception as exc:  # noqa: BLE001
                log.warning("finops collector %s recommendations failed: %s", cls.__name__, exc)
            else:
                batch.recommendations.extend(recs)
                for detector in self._finops_detectors:
                    batch.events.extend(detector.detect(recs))

    def _monitoring_collect(self, provider: Provider, period: Period, batch: Batch) -> None:
        for cls in registry.collectors_for("monitoring", provider.cloud):
            collector = cls(provider)
            # Health checks and metric-anomaly detection are independent: a
            # collector may do one, the other, or both, and one failing must not
            # drop the other.
            try:
                checks = collector.health()
            except Exception as exc:  # noqa: BLE001
                log.warning("monitoring collector %s health failed: %s", cls.__name__, exc)
            else:
                batch.health.extend(checks)
                for detector in self._monitoring_detectors:
                    batch.events.extend(detector.detect(checks))
            # Only some collectors expose metric series (e.g. EC2 CPU/network).
            if hasattr(collector, "collect"):
                try:
                    points = collector.collect(period)
                except Exception as exc:  # noqa: BLE001
                    log.warning("monitoring collector %s collect failed: %s", cls.__name__, exc)
                else:
                    batch.metrics.extend(points)
                    for detector in self._monitoring_metric_detectors:
                        batch.events.extend(detector.detect(points))

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
        """Run a single collect -> detect -> (ship) -> dispatch cycle.

        When an API uplink is configured, the cycle's full batch is shipped to
        the server and any events it returns (server-side forecasts/recommen-
        dations) are dispatched alongside the local ones.
        """
        batch = self._collect_batch()
        events = list(batch.events)
        if self._uplink is not None:
            try:
                events.extend(self._uplink.ship(batch))
            except Exception as exc:  # noqa: BLE001 - the server must not break local dispatch
                log.warning("api uplink failed: %s", exc)
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
            except Exception as exc:  # keep the daemon alive across cycles
                log.exception("cycle failed: %s", exc)
            time.sleep(self._interval)
