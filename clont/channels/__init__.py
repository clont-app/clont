"""Outbound delivery channels and a factory to build them from config."""

from __future__ import annotations

from datetime import timedelta

from clont.channels.base import Channel
from clont.channels.discord import DiscordChannel
from clont.channels.log import LogChannel
from clont.channels.slack import SlackChannel
from clont.channels.telegram import TelegramChannel
from clont.core.config import ChannelsConfig


def _repeat(hours: float | None) -> timedelta | None:
    """Config hours -> repeat window. None/0 means 'notify once, never repeat'."""
    return timedelta(hours=hours) if hours else None


def build(config: ChannelsConfig) -> list[Channel]:
    """Construct the enabled channels. LogChannel is always present."""
    channels: list[Channel] = [
        LogChannel(
            repeat_after=timedelta(hours=config.log.repeat_hours),
            min_severity=config.log.min_severity,
        )
    ]
    if config.slack is not None:
        channels.append(
            SlackChannel(
                config.slack.webhook_url,
                min_severity=config.slack.min_severity,
                repeat_after=_repeat(config.slack.repeat_hours),
            )
        )
    if config.discord is not None:
        channels.append(
            DiscordChannel(
                config.discord.webhook_url,
                min_severity=config.discord.min_severity,
                repeat_after=_repeat(config.discord.repeat_hours),
            )
        )
    if config.telegram is not None:
        channels.append(
            TelegramChannel(
                config.telegram.bot_token,
                config.telegram.chat_id,
                min_severity=config.telegram.min_severity,
                repeat_after=_repeat(config.telegram.repeat_hours),
            )
        )
    return channels


__all__ = [
    "Channel",
    "DiscordChannel",
    "LogChannel",
    "SlackChannel",
    "TelegramChannel",
    "build",
]
