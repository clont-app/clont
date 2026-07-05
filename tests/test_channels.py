"""Channel behavior: severity gating, repeat throttling, per-channel payload
formatting, and the config-driven factory.

The webhook channels post through `clont.channels._http.post_json`; each channel
imports it into its own module namespace, so we monkeypatch that reference and
capture the `(url, payload)` it would have sent. This pins the exact body shape
(the most likely place for a silent breakage) without any network I/O.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from clont.channels import build
from clont.channels.base import AbstractChannel
from clont.channels.discord import DiscordChannel
from clont.channels.log import LogChannel
from clont.channels.slack import SlackChannel
from clont.channels.telegram import TelegramChannel
from clont.core.config import (
    ChannelsConfig,
    DiscordConfig,
    LogConfig,
    SlackConfig,
    TelegramConfig,
)
from clont.core.models import Cloud, CloudResource
from clont.events.models import Event, EventSeverity


def _event(
    severity: EventSeverity = EventSeverity.WARN,
    *,
    key: str = "k1",
    title: str = "CPU high",
    message: str = "instance i-123 at 99%",
) -> Event:
    return Event(
        key=key,
        severity=severity,
        domain="monitoring",
        cloud=Cloud.AWS,
        title=title,
        message=message,
        resource=CloudResource(Cloud.AWS, "ec2", "i-123", region="us-east-1"),
    )


@pytest.fixture
def capture(monkeypatch):
    """Capture post_json calls across all webhook channel modules."""
    calls: list[tuple[str, dict]] = []

    def fake_post_json(url, payload, timeout=10.0):
        calls.append((url, payload))
        return 200

    for mod in ("slack", "discord", "telegram"):
        monkeypatch.setattr(f"clont.channels.{mod}.post_json", fake_post_json)
    return calls


# --- payload formatting -----------------------------------------------------


def test_slack_payload_shape(capture):
    SlackChannel("https://hooks.slack.test/x").send(_event(EventSeverity.CRITICAL))
    assert len(capture) == 1
    url, payload = capture[0]
    assert url == "https://hooks.slack.test/x"
    assert set(payload) == {"text"}
    # emoji + single-asterisk bold title (Slack mrkdwn) + message on its own line
    assert payload["text"] == ":rotating_light: *CPU high*\ninstance i-123 at 99%"


def test_discord_payload_shape(capture):
    DiscordChannel("https://discord.test/wh").send(_event(EventSeverity.WARN))
    url, payload = capture[0]
    assert url == "https://discord.test/wh"
    assert set(payload) == {"content"}
    # Discord bold = double asterisk
    assert payload["content"] == ":warning: **CPU high**\ninstance i-123 at 99%"


def test_telegram_payload_shape_and_url(capture):
    TelegramChannel("123:ABC", "-1001", min_severity=EventSeverity.INFO).send(
        _event(EventSeverity.INFO)
    )
    url, payload = capture[0]
    assert url == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert payload["chat_id"] == "-1001"
    assert payload["parse_mode"] == "HTML"
    assert "<b>CPU high</b>" in payload["text"]


def test_telegram_escapes_html_in_dynamic_text(capture):
    evt = _event(EventSeverity.CRITICAL, title="a<b>&c", message="x < y & z")
    TelegramChannel("t", "c").send(evt)
    text = capture[0][1]["text"]
    # The <b> we inject stays; the user-supplied angle brackets are escaped.
    assert "<b>a&lt;b&gt;&amp;c</b>" in text
    assert "x &lt; y &amp; z" in text


# --- severity gating + repeat throttling ------------------------------------


def test_min_severity_gate_drops_lower(capture):
    ch = SlackChannel("https://x", min_severity=EventSeverity.CRITICAL)
    assert ch.send(_event(EventSeverity.WARN)) is False
    assert ch.send(_event(EventSeverity.CRITICAL)) is True
    assert len(capture) == 1


def test_send_once_never_repeats(capture):
    # repeat_after=None -> deliver a given key exactly once.
    ch = SlackChannel("https://x", min_severity=EventSeverity.INFO, repeat_after=None)
    assert ch.send(_event(key="dup")) is True
    assert ch.send(_event(key="dup")) is False
    # A different condition still gets through.
    assert ch.send(_event(key="other")) is True
    assert len(capture) == 2


def test_repeat_after_allows_resend_once_window_elapses(capture):
    ch = SlackChannel(
        "https://x", min_severity=EventSeverity.INFO, repeat_after=timedelta(hours=1)
    )
    assert ch.send(_event(key="dup")) is True
    assert ch.send(_event(key="dup")) is False   # still throttled
    # Simulate time passing by backdating the last-sent timestamp.
    from datetime import UTC, datetime

    ch._last_sent["dup"] = datetime.now(UTC) - timedelta(hours=2)
    assert ch.send(_event(key="dup")) is True


def test_log_channel_delivers_via_logger(caplog):
    ch = LogChannel(repeat_after=timedelta(hours=1), min_severity=EventSeverity.INFO)
    import logging

    with caplog.at_level(logging.INFO, logger="clont.events"):
        assert ch.send(_event(EventSeverity.WARN)) is True
    assert '"key": "k1"' in caplog.text
    assert '"severity": "warn"' in caplog.text
    assert '"resource": "i-123"' in caplog.text


# --- factory ----------------------------------------------------------------


def test_build_defaults_to_log_only():
    channels = build(ChannelsConfig())
    assert [c.name for c in channels] == ["log"]
    assert all(isinstance(c, AbstractChannel) for c in channels)


def test_build_wires_all_enabled_channels():
    cfg = ChannelsConfig(
        log=LogConfig(repeat_hours=2, min_severity=EventSeverity.INFO),
        slack=SlackConfig(webhook_url="https://s", min_severity=EventSeverity.CRITICAL),
        discord=DiscordConfig(webhook_url="https://d"),
        telegram=TelegramConfig(bot_token="t", chat_id="c", repeat_hours=6),
    )
    channels = build(cfg)
    by_name = {c.name: c for c in channels}
    assert set(by_name) == {"log", "slack", "discord", "telegram"}
    assert isinstance(by_name["slack"], SlackChannel)
    assert by_name["slack"].min_severity == EventSeverity.CRITICAL
    # repeat_hours=None on discord -> send-once (no repeat window)
    assert by_name["discord"].repeat_after is None
    assert by_name["telegram"].repeat_after == timedelta(hours=6)
    assert by_name["log"].repeat_after == timedelta(hours=2)
