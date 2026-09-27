"""Build a ready-to-run Agent from validated config."""

from __future__ import annotations

import clont.finops.aws
import clont.monitoring.aws
from clont import channels
from clont.agent.runner import Agent
from clont.api.uplink import ApiUplink
from clont.core.config import AWSConfig, Config, MetricsConfig
from clont.core.logging import get_logger
from clont.finops.base import FinOpsTuning
from clont.monitoring.base import PER_METRIC_USD, MetricsPolicy
from clont.providers.aws import organizations
from clont.providers.aws.provider import AWSProvider
from clont.providers.base import Provider

log = get_logger("clont.bootstrap")


def _log_cost_source(alias: str, aws, allow_cost_explorer: bool) -> None:
    """Say once, at startup, where this account's spend will come from."""
    if aws.cur is not None:
        log.info("%s: spend from CUR s3://%s/%s", alias, aws.cur.bucket, aws.cur.prefix)
    elif allow_cost_explorer:
        log.warning("%s: spend from Cost Explorer — billed $0.01 per cycle", alias)
    else:
        log.warning(
            "%s: no spend data — set aws.%s.cur (free) or finops.allow_cost_explorer (billed)",
            alias,
            alias,
        )


def _build_metrics_policy(cfg: MetricsConfig) -> MetricsPolicy | None:
    """The CloudWatch budget, or None when the operator hasn't opted in.

    Says out loud what the choice costs — or what it costs you in coverage, since
    the anomaly/threshold/forecast detectors have no other input.
    """
    if not cfg.enabled:
        log.warning(
            "cloudwatch metrics off — anomaly, threshold and disk-full forecast "
            "detection are inert; set monitoring.metrics.enabled (billed per metric)"
        )
        return None
    log.warning(
        "cloudwatch metrics on: up to %d metrics/cycle (~$%.3f per cycle)",
        cfg.max_metrics_per_cycle,
        cfg.max_metrics_per_cycle * PER_METRIC_USD,
    )
    return MetricsPolicy(
        services=frozenset(cfg.services),
        metrics=frozenset(cfg.metrics),
        period_seconds=cfg.period_seconds,
        max_per_cycle=cfg.max_metrics_per_cycle,
    )


def _authenticate(alias: str, aws: AWSConfig) -> AWSProvider | None:
    provider = AWSProvider(alias, aws)
    try:
        provider.authenticate()  # RO role assumption
    except Exception as exc:  # noqa: BLE001 - isolate one bad account
        log.warning("skipping account %s: %s", alias, exc)
        return None
    return provider


def _member_configs(payer: AWSProvider, aws: AWSConfig) -> list[tuple[str, AWSConfig]]:
    """One (alias, config) per org member account to fan out to.

    The role arn is derived from the member's account id and the shared role
    name. Members inherit the payer's regions and external id but never its
    `cur`: spend comes from the payer's report, so a member reading it as well
    would count every line twice.
    """
    members = aws.members
    if members is None:
        return []
    wanted = set(members.include)
    skip = set(members.exclude) | {payer.account_id or ""}
    out: list[tuple[str, AWSConfig]] = []
    for account in organizations.accounts(payer):
        if account.id in skip or (wanted and account.id not in wanted):
            continue
        if account.status != "ACTIVE":  # suspended or closing: nothing to read
            continue
        out.append(
            (
                account.alias,
                aws.model_copy(
                    update={
                        "role_arn": f"arn:aws:iam::{account.id}:role/{members.role_name}",
                        "cur": None,
                        "members": None,
                    }
                ),
            )
        )
    if not out:
        log.warning(
            "%s: members.role_name is set but no member account was discovered — "
            "the role needs organizations:ListAccounts on the payer",
            payer.alias,
        )
    return out


def build_agent(config: Config) -> Agent:
    """Construct authenticated providers + channels.

    Each account is authenticated independently: a bad role is logged and
    skipped so one stale account can't take the whole fleet down. If accounts
    are configured but none authenticate, we abort rather than run blind.
    """
    providers: list[Provider] = []
    members: list[tuple[str, AWSConfig]] = []
    for alias, aws in config.aws.items():
        provider = _authenticate(alias, aws)
        if provider is None:
            continue
        _log_cost_source(alias, aws, config.finops.allow_cost_explorer)
        providers.append(provider)
        members.extend(_member_configs(provider, aws))

    # discovered accounts come second on purpose: an account also named in the
    # YAML keeps that entry, with its own alias and its own cur
    taken = {p.alias for p in providers} | {p.account_id for p in providers}
    for alias, aws in members:
        if alias in taken:
            continue
        provider = _authenticate(alias, aws)
        if provider is None or provider.account_id in taken:
            continue
        log.info("member account %s (%s) discovered", alias, provider.account_id)
        providers.append(provider)
        taken |= {alias, provider.account_id}

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
            allow_cost_explorer=config.finops.allow_cost_explorer,
            allow_cloudwatch_metrics=config.finops.allow_cloudwatch_metrics,
        ),
        showback_unattributed_pct=config.finops.showback_unattributed_pct,
        anomaly_sigma=config.monitoring.anomaly_sigma,
        anomaly_min_points=config.monitoring.anomaly_min_points,
        free_storage_min_pct=config.monitoring.free_storage_min_pct,
        disk_used_max_pct=config.monitoring.disk_used_max_pct,
        cpu_credit_min_balance=config.monitoring.cpu_credit_min_balance,
        swap_usage_max_mb=config.monitoring.swap_usage_max_mb,
        disk_full_forecast_days=config.monitoring.disk_full_forecast_days,
        metrics=_build_metrics_policy(config.monitoring.metrics),
        metrics_interval_seconds=config.monitoring.metrics.collect_every_seconds,
        collect_interval_seconds=config.finops.collect_interval_seconds,
        recommend_interval_seconds=config.finops.recommend_interval_seconds,
        uplink=uplink,
    )
