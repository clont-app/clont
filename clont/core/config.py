"""Configuration: a single YAML file, validated by pydantic-settings.

One `config.yaml` is the whole config — the single source of truth. In
Kubernetes it's a ConfigMap mounted into the pod; locally it's just a file on
disk.

On startup clont validates the file; if none exists it writes one with defaults
(see `ensure`).
"""

from __future__ import annotations

import os
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


class FinOpsConfig(_Model):
    """Cost-event thresholds (Cost Explorer spend digest + spike alerts)."""

    spend_baseline_days: int = 7      # trailing days averaged as the spike baseline
    spend_spike_pct: float = 50.0     # WARN when the latest day exceeds baseline by this %
    spend_min_dollars: float = 1.0    # ignore services whose latest-day spend is below this


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

# Cost-event thresholds (Cost Explorer spend digest + spike alerts).
# finops:
#   spend_baseline_days: 7    # trailing days averaged as the spike baseline
#   spend_spike_pct: 50       # WARN when the latest day exceeds baseline by this %
#   spend_min_dollars: 1      # ignore services below this latest-day spend

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
