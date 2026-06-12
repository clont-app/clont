"""Configuration: a single YAML file, validated by pydantic-settings.

One `config.yaml` is the whole config — the single source of truth. In
Kubernetes it's a ConfigMap mounted into the pod; locally it's just a file on
disk.

On startup clont validates the file; if none exists it writes one with defaults
(see `ensure`).
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from clont.core.logging import LEVEL_NAMES, get_logger
from clont.events.models import EventSeverity

log = get_logger("clont.config")

DEFAULT_CONFIG_PATH = "clont.yaml"


class _Model(BaseModel):
    """Base for config sections: reject unknown keys to catch YAML typos."""

    model_config = ConfigDict(extra="forbid")


# `repeat_hours`: None = notify once per condition; a value re-notifies a
# still-standing condition at most that often.


class LogConfig(_Model):
    """The always-on log channel: full record, with throttled repeats."""

    repeat_hours: float = 3.0
    min_severity: EventSeverity = EventSeverity.INFO


class SlackConfig(_Model):
    webhook_url: str
    min_severity: EventSeverity = EventSeverity.WARN
    repeat_hours: float | None = None


class DiscordConfig(_Model):
    webhook_url: str
    min_severity: EventSeverity = EventSeverity.WARN
    repeat_hours: float | None = None


class TelegramConfig(_Model):
    bot_token: str
    chat_id: str
    min_severity: EventSeverity = EventSeverity.WARN
    repeat_hours: float | None = None


class ChannelsConfig(_Model):
    log: LogConfig = Field(default_factory=LogConfig)   # always on
    slack: SlackConfig | None = None
    discord: DiscordConfig | None = None
    telegram: TelegramConfig | None = None


# --- Clouds (read-only)


class AWSConfig(_Model):
    """Read-only access config for one AWS account.

    On EKS the pod's base identity comes from IRSA; clont assumes this
    cross-account read-only role from there. No static keys live here.
    """

    role_arn: str
    regions: list[str] = Field(default_factory=list)
    external_id: str | None = None


# FinOps


class BudgetRule(_Model):
    """One operator-defined monthly spend budget.

    Matched against month-to-date spend: a `"*"` account matches every account,
    and a null `service` budgets the whole account (otherwise just that one
    Cost Explorer service name).
    """

    account: str = "*"             # account alias, or "*" = every account
    service: str | None = None     # Cost Explorer service name, or None = whole account
    monthly_limit: Decimal         # the budget ceiling for the calendar month
    currency: str = "USD"


class FinOpsConfig(_Model):
    """Cost-event thresholds (spend digest + spike) and recommendation tuning."""

    spend_baseline_days: int = 28     # trailing window (≈4 wks) for the same-weekday spike baseline
    spend_spike_pct: float = 50.0     # WARN when the latest day exceeds baseline by this %
    spend_min_dollars: float = 1.0    # ignore services whose latest-day spend is below this

    # Budgets + month-end forecast (run-rate projection over the daily spend stream)
    budgets: list[BudgetRule] = Field(default_factory=list)  # operator-defined monthly ceilings
    budget_warn_pct: float = 80.0     # WARN when the forecast reaches this % of a budget
    forecast_alpha: float = 0.5       # EWMA recency weight for the daily-rate forecast

    # Idle / stale recommendation thresholds (shared by the idle + snapshot collectors)
    idle_cpu_pct: float = 5.0              # avg CPU % below which a resource is idle
    idle_lookback_days: int = 14           # trailing window the idle averages span
    idle_rds_max_connections: float = 1.0  # avg DB connections below which RDS is idle
    snapshot_max_age_days: int = 90        # EBS snapshots older than this are "old"

    # Commitment (RI/SP) utilization & coverage thresholds
    ri_sp_min_utilization: float = 90.0    # WARN when a commitment is used less than this %
    ri_sp_min_coverage: float = 70.0       # WARN when eligible spend is covered less than this %

    # Governance: non-prod identification (off-hours) + required tag keys (tag hygiene)
    nonprod_tags: dict[str, list[str]] = Field(default_factory=dict)  # tag key -> non-prod values
    required_tags: list[str] = Field(default_factory=list)            # tag keys every resource must carry


class MonitoringConfig(_Model):
    """Monitoring metric-anomaly detection """

    anomaly_sigma: float = 3.0    # WARN when the latest sample is this many σ off baseline
    anomaly_min_points: int = 6   # min baseline samples before a series can flag

    # Default rules: capacity/pressure thresholds + disk-full forecast horizon.
    free_storage_min_pct: float = 10.0    # WARN when free storage drops below this %
    disk_used_max_pct: float = 90.0       # WARN when disk used rises above this %
    cpu_credit_min_balance: float = 20.0  # WARN when a burstable CPU-credit balance falls below this
    swap_usage_max_mb: float = 50.0       # WARN when swap usage exceeds this (MB)
    disk_full_forecast_days: float = 14.0  # WARN when storage is projected full within this many days


# Config

class Config(BaseSettings):
    model_config = SettingsConfigDict(
        extra="forbid",          # reject unknown keys -> catches YAML typos
    )

    interval_seconds: int = 300          # daemon cycle period
    lookback_days: int = 1               # window for cost/metric queries
    log_level: str = "info"              # daemon's own operational verbosity
    aws: dict[str, AWSConfig] = Field(default_factory=dict)   # alias -> account
    finops: FinOpsConfig = Field(default_factory=FinOpsConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, v: str) -> str:
        if v.strip().lower() not in LEVEL_NAMES:
            raise ValueError(f"log_level must be one of {', '.join(LEVEL_NAMES)}")
        return v.strip().lower()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # The YAML file is the only source. `init_settings` is kept so values
        # can still be passed explicitly (e.g. Config(aws=[...]) in tests); the
        # env, dotenv and secrets sources are deliberately dropped.
        sources: list[PydanticBaseSettingsSource] = [init_settings]
        if config_path().is_file():
            sources.append(
                YamlConfigSettingsSource(settings_cls, yaml_file=config_path())
            )
        return tuple(sources)


# A valid, minimal starter config written on first run.
DEFAULT_CONFIG_YAML = """\
# clont configuration (auto-generated defaults). Edit and restart.
# Full reference: clont.example.yaml. Unknown keys are rejected on load.

interval_seconds: 300
lookback_days: 1
log_level: info             # daemon log verbosity: debug|info|warning|error|critical

# Read-only AWS access — uncomment and fill in to start collecting.
# Each account is keyed by an alias (prod/staging/...) shown in notifications:
# aws:
#   prod:
#     role_arn: arn:aws:iam::111111111111:role/clont-readonly
#     regions: [us-east-1]

# FinOps: spend-event thresholds + idle/stale recommendation tuning.
# finops:
#   spend_baseline_days: 28        # trailing window (~4 wks) for same-weekday spike baseline
#   spend_spike_pct: 50            # WARN when the latest day exceeds baseline by this %
#   spend_min_dollars: 1           # ignore services below this latest-day spend
#   budget_warn_pct: 80            # WARN when the month-end forecast reaches this % of a budget
#   forecast_alpha: 0.5            # EWMA recency weight for the daily-rate forecast
#   budgets:                       # monthly ceilings; account "*" = every account
#     - account: prod
#       monthly_limit: 5000        # whole-account budget (omit `service`)
#     - account: prod
#       service: Amazon Elastic Compute Cloud - Compute
#       monthly_limit: 2000        # per-service budget
#   idle_cpu_pct: 5                # avg CPU % below which a resource is idle
#   idle_lookback_days: 14         # trailing window the idle averages span
#   idle_rds_max_connections: 1    # avg DB connections below which RDS is idle
#   snapshot_max_age_days: 90      # EBS snapshots older than this are "old"
#   ri_sp_min_utilization: 90      # WARN when a commitment is used less than this %
#   ri_sp_min_coverage: 70         # WARN when eligible spend is covered less than this %
#   nonprod_tags:                  # tags marking schedulable non-prod resources (off-hours)
#     Environment: [dev, staging, test, qa]
#   required_tags: [Owner, Environment]   # tag keys every cost-bearing resource must carry

# Monitoring metric-anomaly detection + Tier-1 default rules (thresholds + forecast).
# monitoring:
#   anomaly_sigma: 3              # WARN when the latest sample is this many σ off baseline
#   anomaly_min_points: 6         # min baseline samples before a series can flag
#   free_storage_min_pct: 10     # WARN when free storage drops below this % (RDS)
#   disk_used_max_pct: 90        # WARN when disk used rises above this % (Redshift)
#   cpu_credit_min_balance: 20   # WARN when a burstable CPU-credit balance falls below this
#   swap_usage_max_mb: 50        # WARN when swap usage exceeds this in MB (ElastiCache)
#   disk_full_forecast_days: 14  # WARN when storage is projected full within this many days

channels:
  log:                      # always on
    repeat_hours: 3
    min_severity: info
  # slack:
  #   webhook_url: https://hooks.slack.com/services/XXX/YYY/ZZZ
  #   min_severity: warn
  #   repeat_hours: 24
  # discord:
  #   webhook_url: https://discord.com/api/webhooks/XXX/YYY
  # telegram:
  #   bot_token: "123456:ABC-DEF..."
  #   chat_id: "-1001234567890"
"""


def config_path() -> Path:
    """The resolved config file path ($CLONT_CONFIG or ./clont.yaml)."""
    return Path(os.environ.get("CLONT_CONFIG", DEFAULT_CONFIG_PATH))


def ensure() -> Path:
    """Write a default config file if none exists yet. Returns the path.
     A failure to write (read-only filesystem) is logged
    """
    path = config_path()
    if path.is_file():
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG_YAML)
        log.info("wrote default config to %s", path)
    except OSError as exc:
        log.warning("could not write default config to %s: %s", path, exc)
    return path


def load() -> Config:
    """Load and validate the configuration from YAML"""
    return Config()
