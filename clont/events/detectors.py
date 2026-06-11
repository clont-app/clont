"""Detectors: turn collector output into Events.

Each detector is narrow and pure (inputs -> events), which keeps them easy to
unit-test without touching a cloud. The agent feeds collector results through
the relevant detectors every cycle.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from clont.core.models import Cloud, CloudResource
from clont.events.models import Event, EventSeverity
from clont.events.stats import mean, median, modified_zscore
from clont.finops.models import CostRecord, Recommendation
from clont.monitoring.models import HealthCheck, HealthStatus, MetricPoint

# Minimum same-phase samples (same weekday / same hour-of-day) before a detector
# trusts a seasonal baseline; below this it falls back to the flat baseline.
_MIN_SEASONAL_SAMPLES = 2


class RecommendationDetector:
    """FinOps cost recommendations -> one event each."""

    def detect(self, recommendations: list[Recommendation]) -> list[Event]:
        events: list[Event] = []
        for rec in recommendations:
            r = rec.resource
            alias = r.alias or "-"
            savings = rec.estimated_savings
            # Some recommendations (e.g. idle instances) have no per-resource
            savings_txt = (
                f" (est. savings {savings.amount} {savings.currency}/mo)"
                if savings.amount > 0
                else ""
            )
            events.append(
                Event(
                    key=f"finops:rec:{alias}:{r.cloud}:{r.service}:{rec.kind}:{r.resource_id}",
                    severity=EventSeverity.WARN,
                    domain="finops",
                    cloud=r.cloud,
                    title=f"[{alias}] Cost recommendation: {rec.service}",
                    message=f"{rec.summary}{savings_txt}",
                    resource=r,
                    payload={
                        "estimated_savings": str(savings.amount),
                        "currency": savings.currency,
                    },
                )
            )
        return events


class SpendDigestDetector:
    """Daily spend -> one INFO digest event per account (latest day present).
    """

    _TOP_N = 5

    def detect(self, records: list[CostRecord]) -> list[Event]:
        by_alias: dict[str | None, list[CostRecord]] = defaultdict(list)
        for rec in records:
            by_alias[rec.alias].append(rec)

        events: list[Event] = []
        for alias_key, recs in by_alias.items():
            latest_day = max(r.period.end for r in recs)
            today = [r for r in recs if r.period.end == latest_day]
            total = sum((r.cost.amount for r in today), Decimal(0))
            currency = today[0].cost.currency
            alias = alias_key or "-"

            top = sorted(today, key=lambda r: r.cost.amount, reverse=True)[: self._TOP_N]
            breakdown = ", ".join(f"{r.service} {r.cost.amount}" for r in top)
            events.append(
                Event(
                    key=f"finops:spend:digest:{alias}",
                    severity=EventSeverity.INFO,
                    domain="finops",
                    cloud=Cloud.AWS,
                    title=f"[{alias}] Daily spend: {total} {currency}",
                    message=f"{latest_day}: {total} {currency} — top: {breakdown}",
                    payload={
                        "day": latest_day.isoformat(),
                        "total": str(total),
                        "currency": currency,
                        "services": {r.service: str(r.cost.amount) for r in today},
                    },
                )
            )
        return events


class SpendSpikeDetector:
    """Daily spend -> WARN when a service's latest day jumps over its baseline.

    The "latest day" is the most recent day in the dataset for the *account*
    (not per service), so a service that has gone quiet can't keep alerting on a
    stale day. A service with no prior days (new account / short window) can't spike.
    """

    def __init__(self, spike_pct: float = 50.0, min_dollars: float = 1.0) -> None:
        self._factor = Decimal(1) + Decimal(str(spike_pct)) / Decimal(100)
        self._min = Decimal(str(min_dollars))

    def detect(self, records: list[CostRecord]) -> list[Event]:
        by_alias: dict[str | None, list[CostRecord]] = defaultdict(list)
        for rec in records:
            by_alias[rec.alias].append(rec)

        events: list[Event] = []
        for alias_key, recs in by_alias.items():
            anchor = max(r.period.end for r in recs)  # latest day for this account
            series: dict[str, dict] = defaultdict(dict)
            currency: dict[str, str] = {}
            for rec in recs:
                day = rec.period.end
                series[rec.service][day] = series[rec.service].get(day, Decimal(0)) + rec.cost.amount
                currency[rec.service] = rec.cost.currency

            for service, by_day in series.items():
                latest = by_day.get(anchor)
                if latest is None:  # service had no spend on the anchor day
                    continue
                prior = [amt for day, amt in by_day.items() if day < anchor]
                if not prior:
                    continue

                # Seasonal baseline: compare the anchor day against prior days of
                # the *same weekday* (a normal Monday vs other Mondays) so weekly
                # cycles don't read as spikes.
                same_weekday = [
                    amt for day, amt in by_day.items()
                    if day < anchor and day.weekday() == anchor.weekday()
                ]
                if len(same_weekday) >= _MIN_SEASONAL_SAMPLES:
                    baseline = median(same_weekday)
                    basis = "same-weekday median"
                else:
                    baseline = mean(prior)
                    basis = "avg"

                if baseline <= 0 or latest < self._min or latest <= baseline * self._factor:
                    continue

                alias = alias_key or "-"
                pct = (latest - baseline) / baseline * 100
                cur = currency[service]
                events.append(
                    Event(
                        key=f"finops:spend:spike:{alias}:aws:{service}",
                        severity=EventSeverity.WARN,
                        domain="finops",
                        cloud=Cloud.AWS,
                        title=f"[{alias}] Spend spike: {service}",
                        message=f"{latest} {cur} vs {baseline:.2f} {basis} (+{pct:.0f}%)",
                        resource=CloudResource(
                            cloud=Cloud.AWS,
                            service=service,
                            resource_id=service,
                            alias=alias_key,
                        ),
                        payload={
                            "latest": str(latest),
                            "baseline": str(baseline),
                            "currency": cur,
                        },
                    )
                )
        return events


class MetricAnomalyDetector:
    """Monitoring metric series -> WARN when the latest point deviates sharply.

    Anomaly (not rule-based threshold) detection: per (resource, metric) series,
    flag the latest point if it sits more than `sigma` deviations from its
    baseline — two-sided (a sharp drop is as notable as a spike).


    A series needs at least `min_points` baseline samples, and a baseline/cohort
    with zero spread is skipped (no meaningful σ — alerting on every micro-change
    would be pure noise).
    """

    def __init__(self, sigma: float = 3.0, min_points: int = 6) -> None:
        self._sigma = sigma
        self._min_points = min_points

    def detect(self, points: list[MetricPoint]) -> list[Event]:
        series: dict[tuple, list[MetricPoint]] = defaultdict(list)
        for p in points:
            r = p.resource
            if r is None:
                continue  # can't key an anomaly without a resource
            series[(r.alias, r.cloud, r.service, r.resource_id, p.name)].append(p)

        events: list[Event] = []
        for (alias_key, cloud, service, rid, metric), pts in series.items():
            if len(pts) <= self._min_points:  # need a baseline plus the latest
                continue
            pts.sort(key=lambda p: p.timestamp)
            latest = pts[-1]
            baseline = pts[:-1]

            # Seasonal cohort: score the latest sample against prior samples at
            # the *same hour-of-day* (handles daily cycles — business hours vs
            # night) using a robust median+MAD score. Fall back to a flat
            # mean/std z-score when there isn't enough same-hour history (e.g. a
            # single day of points), which preserves the original behaviour.
            cohort = [p.value for p in baseline if p.timestamp.hour == latest.timestamp.hour]
            if len(cohort) >= _MIN_SEASONAL_SAMPLES:
                score = modified_zscore(latest.value, cohort)
                if score is None:  # flat cohort -> no meaningful deviation
                    continue
                center = median(cohort)
                basis = "same-hour"
            else:
                values = [p.value for p in baseline]
                avg = mean(values)
                std = (sum((v - avg) ** 2 for v in values) / len(values)) ** 0.5
                if std <= 0:  # flat baseline -> no meaningful deviation
                    continue
                score = (latest.value - avg) / std
                center = avg
                basis = "baseline"

            if abs(score) < self._sigma:
                continue

            alias = alias_key or "-"
            direction = "above" if score > 0 else "below"
            events.append(
                Event(
                    key=f"monitoring:anomaly:{alias}:{cloud}:{service}:{rid}:{metric}",
                    severity=EventSeverity.WARN,
                    domain="monitoring",
                    cloud=cloud,
                    title=f"[{alias}] Metric anomaly: {service} {metric}",
                    message=(
                        f"{metric} {latest.value:.2f} {latest.unit} is "
                        f"{abs(score):.1f}σ {direction} {basis} {center:.2f} "
                        f"(n={len(baseline)})"
                    ),
                    resource=latest.resource,
                    payload={
                        "metric": metric,
                        "latest": str(latest.value),
                        "baseline_center": f"{center:.4f}",
                        "sigma": f"{score:.2f}",
                        "basis": basis,
                    },
                )
            )
        return events


class HealthDetector:
    """Monitoring health checks -> events for anything not OK."""

    _SEVERITY = {
        HealthStatus.WARN: EventSeverity.WARN,
        HealthStatus.CRITICAL: EventSeverity.CRITICAL,
    }

    def detect(self, checks: list[HealthCheck]) -> list[Event]:
        events: list[Event] = []
        for chk in checks:
            severity = self._SEVERITY.get(chk.status)
            if severity is None:  # OK / UNKNOWN -> not an event
                continue
            r = chk.resource
            alias = r.alias or "-"
            events.append(
                Event(
                    key=f"monitoring:health:{alias}:{r.cloud}:{r.service}:{r.resource_id}",
                    severity=severity,
                    domain="monitoring",
                    cloud=r.cloud,
                    title=f"[{alias}] Health {chk.status}: {r.service}",
                    message=chk.summary,
                    resource=r,
                )
            )
        return events
