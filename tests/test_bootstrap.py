"""Bootstrap isolates per-account auth failures (skip-and-continue)."""

from __future__ import annotations

import pytest

from clont.agent.bootstrap import build_agent
from clont.core.config import AWSConfig, Config

_AUTH = "clont.providers.aws.provider.AWSProvider.authenticate"


def _config(*aliases: str) -> Config:
    return Config(aws={a: AWSConfig(role_arn=f"arn:aws:iam::0:role/{a}") for a in aliases})


def test_one_bad_account_is_skipped(monkeypatch):
    # prod authenticates, staging raises -> only prod survives.
    def fake_auth(self):
        if self.alias == "staging":
            raise RuntimeError("cannot assume role")
        self.account_id = "111111111111"

    monkeypatch.setattr(_AUTH, fake_auth)

    agent = build_agent(_config("prod", "staging"))
    assert [p.alias for p in agent._providers] == ["prod"]


def test_all_bad_accounts_raise(monkeypatch):
    def fake_auth(self):
        raise RuntimeError("nope")

    monkeypatch.setattr(_AUTH, fake_auth)

    with pytest.raises(RuntimeError, match="no configured accounts"):
        build_agent(_config("prod", "staging"))


def test_no_accounts_is_fine(monkeypatch):
    # An empty fleet is valid (log channel still runs); must not raise.
    agent = build_agent(Config())
    assert agent._providers == []
