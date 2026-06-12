"""Detectors: turn collector output into Events.

Each detector is narrow and pure (inputs -> events), which keeps them easy to
unit-test without touching a cloud. The agent feeds collector results through
the relevant detectors every cycle.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from clont.core.config import BudgetRule
from clont.core.models import Cloud, CloudResource
from clont.events.models import Event, EventSeverity
from clont.events.stats import (
    mean,
    median,
    modified_zscore,
    periods_to_cross,
    project_month_end,
)
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


@dataclass(frozen=True, slots=True)
class _AccountMonth:
    """Per-account current-month spend, bucketed by day (for forecast/budgets)."""

    alias: str | None
    anchor: date                                   # latest day seen for the account
    currency: str
    account_daily: dict[date, Decimal]             # day -> account total
    service_daily: dict[str, dict[date, Decimal]]  # service -> {day -> total}

    def days_in_month(self) -> int:
        return calendar.monthrange(self.anchor.year, self.anchor.month)[1]


def _account_months(records: list[CostRecord]) -> list[_AccountMonth]:
    """Reduce the daily cost stream to each account's current-(latest-)month spend.

    The "current month" is the month of the most recent day present for that
    account, so partial trailing days from the prior month are dropped — the
    forecast and budgets only reason about the month in progress.
    """
    by_alias: dict[str | None, list[CostRecord]] = defaultdict(list)
    for rec in records:
        by_alias[rec.alias].append(rec)

    out: list[_AccountMonth] = []
    for alias_key, recs in by_alias.items():
        anchor = max(r.period.end for r in recs)
        ym = (anchor.year, anchor.month)
        account_daily: dict[date, Decimal] = defaultdict(Decimal)
        service_daily: dict[str, dict[date, Decimal]] = defaultdict(
            lambda: defaultdict(Decimal)
        )
        currency = "USD"
        for rec in recs:
            day = rec.period.end
            if (day.year, day.month) != ym:
                continue
            account_daily[day] += rec.cost.amount
            service_daily[rec.service][day] += rec.cost.amount
            currency = rec.cost.currency
        if not account_daily:
            continue
        out.append(
            _AccountMonth(
                alias=alias_key,
                anchor=anchor,
                currency=currency,
                account_daily=dict(account_daily),
                service_daily={s: dict(d) for s, d in service_daily.items()},
            )
        )
    return out


def _ordered(daily: dict[date, Decimal]) -> list[Decimal]:
    """Daily totals ordered oldest-first (the shape `project_month_end` wants)."""
    return [daily[d] for d in sorted(daily)]


class SpendForecastDetector:
    """Daily spend -> one INFO month-end forecast per account.

    Run-rate projection: month-to-date actual plus an EWMA daily rate applied to
    the remaining days of the calendar month.
    """

    def __init__(self, alpha: float = 0.5) -> None:
        self._alpha = alpha

    def detect(self, records: list[CostRecord]) -> list[Event]:
        events: list[Event] = []
        for acc in _account_months(records):
            days = acc.days_in_month()
            mtd = sum(acc.account_daily.values(), Decimal(0))
            forecast = project_month_end(_ordered(acc.account_daily), days, self._alpha)
            alias = acc.alias or "-"
            cur = acc.currency
            events.append(
                Event(
                    key=f"finops:spend:forecast:{alias}",
                    severity=EventSeverity.INFO,
                    domain="finops",
                    cloud=Cloud.AWS,
                    title=f"[{alias}] Month-end forecast: ~{forecast:.0f} {cur}",
                    message=(
                        f"{acc.anchor.year}-{acc.anchor.month:02d}: {mtd:.2f} {cur} "
                        f"MTD over {len(acc.account_daily)}/{days} days "
                        f"-> ~{forecast:.0f} {cur} projected"
                    ),
                    payload={
                        "mtd": str(mtd),
                        "forecast": str(forecast),
                        "currency": cur,
                        "days_elapsed": str(len(acc.account_daily)),
                        "days_in_month": str(days),
                    },
                )
            )
        return events


class BudgetDetector:
    """Daily spend vs operator-defined monthly budgets.

    For each budget rule, compares month-to-date spend (whole account or one
    service) against the limit and its run-rate forecast:
      * MTD already at/over the limit  -> CRITICAL (actual breach)
      * forecast at/over the limit      -> WARN (projected breach)
      * forecast at/over `warn_pct`% of limit -> WARN (approaching)
    A `"*"` rule fans out to every account; a null `service` budgets the account.
    """

    def __init__(
        self, budgets: list[BudgetRule], warn_pct: float = 80.0, alpha: float = 0.5
    ) -> None:
        self._budgets = budgets
        self._warn = Decimal(str(warn_pct)) / Decimal(100)
        self._alpha = alpha

    def detect(self, records: list[CostRecord]) -> list[Event]:
        if not self._budgets:
            return []
        accounts = _account_months(records)
        events: list[Event] = []
        for rule in self._budgets:
            for acc in accounts:
                if rule.account not in ("*", acc.alias):
                    continue
                daily = (
                    acc.account_daily
                    if rule.service is None
                    else acc.service_daily.get(rule.service)
                )
                if not daily:  # this account has no spend for the budgeted service
                    continue
                event = self._evaluate(rule, acc, daily)
                if event is not None:
                    events.append(event)
        return events

    def _evaluate(
        self, rule: BudgetRule, acc: _AccountMonth, daily: dict[date, Decimal]
    ) -> Event | None:
        limit = rule.monthly_limit
        if limit <= 0:
            return None
        mtd = sum(daily.values(), Decimal(0))
        forecast = project_month_end(_ordered(daily), acc.days_in_month(), self._alpha)

        if mtd >= limit:
            severity = EventSeverity.CRITICAL
            state = f"over budget — {mtd:.2f} of {limit} {rule.currency} spent"
        elif forecast >= limit:
            severity = EventSeverity.WARN
            state = f"forecast {forecast:.0f} {rule.currency} exceeds {limit} budget"
        elif forecast >= limit * self._warn:
            severity = EventSeverity.WARN
            pct = forecast / limit * 100
            state = f"forecast {forecast:.0f} {rule.currency} is {pct:.0f}% of {limit} budget"
        else:
            return None

        alias = acc.alias or "-"
        scope = rule.service or "account"
        return Event(
            key=f"finops:budget:{alias}:{scope}",
            severity=severity,
            domain="finops",
            cloud=Cloud.AWS,
            title=f"[{alias}] Budget {scope}: {state}",
            message=(
                f"{scope}: {mtd:.2f} {rule.currency} MTD, "
                f"~{forecast:.0f} forecast vs {limit} {rule.currency} budget"
            ),
            payload={
                "scope": scope,
                "mtd": str(mtd),
                "forecast": str(forecast),
                "limit": str(limit),
                "currency": rule.currency,
            },
        )


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


@dataclass(frozen=True, slots=True)
class _Rule:
    """One threshold rule: fire when the latest value breaches `limit`."""

    metric: str
    over: bool          # True -> fire when value > limit; False -> when value < limit
    limit: float
    label: str          # human phrase, e.g. "free storage"


class ThresholdRuleDetector:
    """Monitoring metric series -> WARN when the latest point breaches a default rule.

    Opinionated, deterministic threshold checks (not anomaly detection) over the
    normalized capacity/pressure metrics the collectors emit: low free storage,
    high disk used, depleted CPU credits, swap pressure. One global threshold per
    metric (operator-tunable) rather than a per-resource number, per the Tier-1
    "curated defaults" principle. Only the latest sample of each series is judged.
    """

    def __init__(
        self,
        free_storage_min_pct: float = 10.0,
        disk_used_max_pct: float = 90.0,
        cpu_credit_min_balance: float = 20.0,
        swap_usage_max_mb: float = 50.0,
    ) -> None:
        self._rules = [
            _Rule("FreeStoragePercent", False, free_storage_min_pct, "free storage"),
            _Rule("PercentageDiskSpaceUsed", True, disk_used_max_pct, "disk used"),
            _Rule("CPUCreditBalance", False, cpu_credit_min_balance, "CPU credit balance"),
            _Rule("SwapUsage", True, swap_usage_max_mb, "swap usage"),
        ]
        self._by_metric = {r.metric: r for r in self._rules}

    def detect(self, points: list[MetricPoint]) -> list[Event]:
        series: dict[tuple, list[MetricPoint]] = defaultdict(list)
        for p in points:
            if p.resource is None or p.name not in self._by_metric:
                continue
            r = p.resource
            series[(r.alias, r.cloud, r.service, r.resource_id, p.name)].append(p)

        events: list[Event] = []
        for (alias_key, cloud, service, rid, metric), pts in series.items():
            rule = self._by_metric[metric]
            latest = max(pts, key=lambda p: p.timestamp)
            breached = latest.value > rule.limit if rule.over else latest.value < rule.limit
            if not breached:
                continue
            alias = alias_key or "-"
            cmp = "above" if rule.over else "below"
            events.append(
                Event(
                    key=f"monitoring:rule:{alias}:{cloud}:{service}:{rid}:{metric}",
                    severity=EventSeverity.WARN,
                    domain="monitoring",
                    cloud=cloud,
                    title=f"[{alias}] {service} {rule.label}: {latest.value:.1f} {latest.unit}",
                    message=(
                        f"{rule.label} {latest.value:.1f} {latest.unit} is {cmp} the "
                        f"{rule.limit:g} threshold"
                    ),
                    resource=latest.resource,
                    payload={
                        "metric": metric,
                        "value": str(latest.value),
                        "threshold": str(rule.limit),
                    },
                )
            )
        return events


class CapacityForecastDetector:
    """Monitoring storage series -> WARN when a linear trend predicts exhaustion soon.

    "Predicted disk-full in N days": for fill metrics it fits a least-squares line
    over the recent window and projects when the trend reaches capacity
    (`FreeStoragePercent` -> 0, `PercentageDiskSpaceUsed` -> 100). Fires only when
    the projected crossing is within `forecast_days` *and* the series is genuinely
    heading there (a flat or improving trend is silent — no false alarms). Needs at
    least `min_points` samples so a noisy two-point slope can't fire.
    """

    _SECONDS_PER_DAY = 86400.0
    # metric -> the value it is "full" at.
    _CAPACITY = {"FreeStoragePercent": 0.0, "PercentageDiskSpaceUsed": 100.0}

    def __init__(self, forecast_days: float = 14.0, min_points: int = 4) -> None:
        self._forecast_days = forecast_days
        self._min_points = min_points

    def detect(self, points: list[MetricPoint]) -> list[Event]:
        series: dict[tuple, list[MetricPoint]] = defaultdict(list)
        for p in points:
            if p.resource is None or p.name not in self._CAPACITY:
                continue
            r = p.resource
            series[(r.alias, r.cloud, r.service, r.resource_id, p.name)].append(p)

        events: list[Event] = []
        for (alias_key, cloud, service, rid, metric), pts in series.items():
            if len(pts) < self._min_points:
                continue
            pts.sort(key=lambda p: p.timestamp)
            xs = [p.timestamp.timestamp() for p in pts]
            ys = [p.value for p in pts]
            capacity = self._CAPACITY[metric]
            lead_seconds = periods_to_cross(xs, ys, capacity)
            if lead_seconds is None:
                continue
            lead_days = lead_seconds / self._SECONDS_PER_DAY
            if lead_days > self._forecast_days:
                continue
            latest = pts[-1]
            alias = alias_key or "-"
            events.append(
                Event(
                    key=f"monitoring:forecast:{alias}:{cloud}:{service}:{rid}:{metric}",
                    severity=EventSeverity.WARN,
                    domain="monitoring",
                    cloud=cloud,
                    title=f"[{alias}] {service} predicted full in ~{lead_days:.0f}d",
                    message=(
                        f"{metric} at {latest.value:.1f} {latest.unit} and trending to "
                        f"capacity — projected full in ~{lead_days:.1f} days "
                        f"(n={len(pts)})"
                    ),
                    resource=latest.resource,
                    payload={
                        "metric": metric,
                        "latest": str(latest.value),
                        "days_to_full": f"{lead_days:.2f}",
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
