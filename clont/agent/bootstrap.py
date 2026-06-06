"""Build a ready-to-run Agent from validated config."""

from __future__ import annotations

from clont import channels
from clont.agent.runner import Agent
from clont.core.config import Config
from clont.core.logging import get_logger
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

    return Agent(
        providers,
        channels.build(config.channels),
        interval_seconds=config.interval_seconds,
        lookback_days=config.lookback_days,
    )
