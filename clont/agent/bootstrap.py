"""Build a ready-to-run Agent from validated config."""

from __future__ import annotations

import clont.finops.aws
import clont.monitoring.aws
from clont import channels
from clont.agent.runner import Agent
from clont.api.uplink import ApiUplink
from clont.core.config import Config
from clont.core.logging import get_logger
from clont.finops.base import FinOpsTuning
from clont.providers.aws.provider import AWSProvider
from clont.providers.base import Provider

log = get_logger("clont.bootstrap")


def build_agent(config: Config) -> Agent:
    """Construct authenticated providers + channels.

    Each account is authenticated independently: a bad role is logged and
    skipped so one stale account can't take the whole fleet down. If accounts
    are configured but none authenticate, we abort rather than run blind.
    """
    providers: list[Provider] = []
    for alias, aws in config.aws.items():
        provider = AWSProvider(alias, aws)
        try:
            provider.authenticate()  # RO role assumption
        except Exception as exc:  # noqa: BLE001 - isolate one bad account
            log.warning("skipping account %s: %s", alias, exc)
            continue
        providers.append(provider)

    if config.aws and not providers:
        raise RuntimeError("no configured accounts could be authenticated")

    uplink = (
        ApiUplink(config.api.url, config.api.api_key, timeout=config.api.timeout_seconds)
        if config.api is not None
        else None
    )

    return Agent(
        providers,
        channels.build(config.channels),
        interval_seconds=config.interval_seconds,
        lookback_days=config.lookback_days,
        spend_baseline_days=config.finops.spend_baseline_days,
        spend_spike_pct=config.finops.spend_spike_pct,
        spend_min_dollars=config.finops.spend_min_dollars,
        budgets=config.finops.budgets,
        budget_warn_pct=config.finops.budget_warn_pct,
        forecast_alpha=config.finops.forecast_alpha,
        finops_tuning=FinOpsTuning(
            idle_cpu_pct=config.finops.idle_cpu_pct,
            idle_lookback_days=config.finops.idle_lookback_days,
            idle_rds_max_connections=config.finops.idle_rds_max_connections,
            snapshot_max_age_days=config.finops.snapshot_max_age_days,
            ri_sp_min_utilization=config.finops.ri_sp_min_utilization,
            ri_sp_min_coverage=config.finops.ri_sp_min_coverage,
            nonprod_tags={k: tuple(v) for k, v in config.finops.nonprod_tags.items()},
            required_tags=tuple(config.finops.required_tags),
        ),
        anomaly_sigma=config.monitoring.anomaly_sigma,
        anomaly_min_points=config.monitoring.anomaly_min_points,
        free_storage_min_pct=config.monitoring.free_storage_min_pct,
        disk_used_max_pct=config.monitoring.disk_used_max_pct,
        cpu_credit_min_balance=config.monitoring.cpu_credit_min_balance,
        swap_usage_max_mb=config.monitoring.swap_usage_max_mb,
        disk_full_forecast_days=config.monitoring.disk_full_forecast_days,
        uplink=uplink,
    )
