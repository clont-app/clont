"""Build a ready-to-run Agent from validated config."""

from __future__ import annotations

from clont import channels
from clont.agent.runner import Agent
from clont.core.config import Config
from clont.providers.aws.provider import AWSProvider
from clont.providers.base import Provider


def build_agent(config: Config) -> Agent:
    """Construct providers (authenticated) + channels"""
    providers: list[Provider] = [AWSProvider(aws) for aws in config.aws]
    for provider in providers:
        provider.authenticate()  # RO role assumption

    return Agent(
        providers,
        channels.build(config.channels),
        interval_seconds=config.interval_seconds,
        lookback_days=config.lookback_days,
    )
