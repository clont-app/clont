"""CLI smoke tests: the daemon's only user entrypoint.

These assert wiring, not behavior of the underlying collectors — that `clont
config check` validates and summarizes config, and that `clont run` builds an
agent and drives the right loop (`--once` vs. forever) without executing
anything long-running. `build_agent` is stubbed so no cloud auth happens.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from clont.cli import run as run_mod
from clont.cli.app import app

runner = CliRunner()


def _write_config(tmp_path, body: str):
    path = tmp_path / "clont.yaml"
    path.write_text(body)
    return path


# --- config check -----------------------------------------------------------


def test_config_check_ok_summary(tmp_path):
    cfg = _write_config(
        tmp_path,
        "interval_seconds: 120\n"
        "log_level: debug\n"
        "aws:\n"
        "  prod:\n"
        "    role_arn: arn:aws:iam::111111111111:role/clont-readonly\n"
        "channels:\n"
        "  slack:\n"
        "    webhook_url: https://hooks.slack.com/services/x/y/z\n",
    )
    result = runner.invoke(app, ["config", "check", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output
    assert "prod" in result.output
    assert "log+slack" in result.output
    assert "interval=120s" in result.output
    assert "log_level=debug" in result.output


def test_config_check_defaults_to_log_only(tmp_path):
    cfg = _write_config(tmp_path, "interval_seconds: 300\n")
    result = runner.invoke(app, ["config", "check", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "channels: log;" in result.output
    assert "none" in result.output          # no AWS accounts


def test_config_check_invalid_exits_nonzero(tmp_path):
    # Unknown top-level key -> validation error surfaced, exit code 1.
    cfg = _write_config(tmp_path, "bogus_key: 1\n")
    result = runner.invoke(app, ["config", "check", "--config", str(cfg)])
    assert result.exit_code == 1
    assert "config invalid" in result.output


def test_config_check_honors_env_var(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path, "interval_seconds: 42\n")
    monkeypatch.setenv("CLONT_CONFIG", str(cfg))
    result = runner.invoke(app, ["config", "check"])
    assert result.exit_code == 0, result.output
    assert "interval=42s" in result.output


# --- run --------------------------------------------------------------------


class _FakeAgent:
    def __init__(self) -> None:
        self.once = 0
        self.forever = 0

    def run_once(self) -> None:
        self.once += 1

    def run_forever(self) -> None:
        self.forever += 1


@pytest.fixture
def fake_agent(tmp_path, monkeypatch):
    agent = _FakeAgent()
    monkeypatch.setattr(run_mod, "build_agent", lambda settings: agent)
    # Point at an existing config so ensure() doesn't write into the cwd.
    cfg = _write_config(tmp_path, "interval_seconds: 300\n")
    monkeypatch.setenv("CLONT_CONFIG", str(cfg))
    return agent


def test_run_once_drives_single_cycle(fake_agent):
    result = runner.invoke(app, ["run", "--once"])
    assert result.exit_code == 0, result.output
    assert fake_agent.once == 1
    assert fake_agent.forever == 0


def test_run_without_once_drives_forever(fake_agent):
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0, result.output
    assert fake_agent.forever == 1
    assert fake_agent.once == 0


def test_run_config_flag_sets_env(tmp_path, monkeypatch):
    agent = _FakeAgent()
    monkeypatch.setattr(run_mod, "build_agent", lambda settings: agent)
    monkeypatch.delenv("CLONT_CONFIG", raising=False)
    cfg = _write_config(tmp_path, "interval_seconds: 77\n")

    result = runner.invoke(app, ["run", "--once", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert agent.once == 1


def test_bare_app_shows_help():
    # no_args_is_help -> invoking with nothing prints usage, not an error trace.
    result = runner.invoke(app, [])
    assert "run" in result.output
    assert "config" in result.output
