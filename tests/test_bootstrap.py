"""Bootstrap: per-account auth failures are isolated, org members are discovered."""

from __future__ import annotations

import pytest

from clont.agent.bootstrap import build_agent
from clont.core.config import AWSConfig, Config, CURConfig, MembersConfig
from clont.providers.k8s.config import KubernetesCluster
from clont.providers.aws import organizations
from clont.providers.aws.organizations import OrgAccount

_AUTH = "clont.providers.aws.provider.AWSProvider.authenticate"
_ACCOUNTS = "clont.providers.aws.organizations.accounts"


def _config(*aliases: str) -> Config:
    return Config(aws={a: AWSConfig(role_arn=f"arn:aws:iam::0:role/{a}") for a in aliases})


@pytest.fixture(autouse=True)
def _no_org_cache():
    organizations.clear_cache()
    yield
    organizations.clear_cache()


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


# --- organizations fan-out


def _auth_from_arn(self):
    self.account_id = self._config.role_arn.split(":")[4]


_ORG = [
    OrgAccount(id="111111111111", alias="payer", name="Payer", status="ACTIVE"),
    OrgAccount(id="222222222222", alias="sandbox-team", name="Sandbox Team", status="ACTIVE"),
    OrgAccount(id="333333333333", alias="old", name="old", status="SUSPENDED"),
]


def _payer_config(**members) -> Config:
    return Config(
        aws={
            "payer": AWSConfig(
                role_arn="arn:aws:iam::111111111111:role/clont-readonly",
                cur=CURConfig(bucket="billing", report_name="clont-cur", include_linked=True),
                members=MembersConfig(role_name="clont-readonly", **members),
            )
        }
    )


def test_org_members_are_discovered_and_get_no_cur_of_their_own(monkeypatch):
    monkeypatch.setattr(_AUTH, _auth_from_arn)
    monkeypatch.setattr(_ACCOUNTS, lambda provider: _ORG)

    agent = build_agent(_payer_config())

    # payer keeps its yaml alias and its report; the suspended account is left out
    assert [p.alias for p in agent._providers] == ["payer", "sandbox-team"]
    member = agent._providers[1]
    assert member._config.role_arn == "arn:aws:iam::222222222222:role/clont-readonly"
    assert member.cur is None  # else the payer report would be counted twice
    assert member._config.members is None  # no recursive fan-out


def test_include_and_exclude_narrow_the_fan_out(monkeypatch):
    monkeypatch.setattr(_AUTH, _auth_from_arn)
    monkeypatch.setattr(_ACCOUNTS, lambda provider: _ORG)

    assert [p.alias for p in build_agent(_payer_config(exclude=["222222222222"]))._providers] == [
        "payer"
    ]
    assert [p.alias for p in build_agent(_payer_config(include=["999"]))._providers] == ["payer"]


def test_an_explicitly_configured_member_is_not_duplicated(monkeypatch):
    monkeypatch.setattr(_AUTH, _auth_from_arn)
    monkeypatch.setattr(_ACCOUNTS, lambda provider: _ORG)

    config = _payer_config()
    # same account id, spelled out in the yaml under a different alias
    config.aws["sandbox"] = AWSConfig(role_arn="arn:aws:iam::222222222222:role/other")

    agent = build_agent(config)

    assert [p.alias for p in agent._providers] == ["payer", "sandbox"]
    assert agent._providers[1]._config.role_arn.endswith("role/other")


def test_a_member_role_that_cannot_be_assumed_is_skipped(monkeypatch):
    def fake_auth(self):
        if "222222222222" in self._config.role_arn:
            raise RuntimeError("no trust policy")
        _auth_from_arn(self)

    monkeypatch.setattr(_AUTH, fake_auth)
    monkeypatch.setattr(_ACCOUNTS, lambda provider: _ORG)

    agent = build_agent(_payer_config())

    assert [p.alias for p in agent._providers] == ["payer"]


def test_a_cluster_whose_provider_did_not_come_up_is_reported_not_fatal(monkeypatch, caplog):
    # the alias is configured — the config validator checked that — so this is its account
    # failing auth, and a cluster nobody can price must not take the rest of the run down
    def fake_auth(self):
        if self.alias == "prod":
            raise RuntimeError("cannot assume role")
        _auth_from_arn(self)

    monkeypatch.setattr(_AUTH, fake_auth)
    config = Config(
        aws={
            "prod": AWSConfig(role_arn="arn:aws:iam::111111111111:role/prod"),
            "dev": AWSConfig(role_arn="arn:aws:iam::222222222222:role/dev"),
        },
        kubernetes={"lab": KubernetesCluster(priced_by="prod")},
    )
    with caplog.at_level("WARNING"):
        agent = build_agent(config)
    assert [p.alias for p in agent._providers] == ["dev"]
    assert "nothing prices it" in caplog.text


def test_an_unreachable_cluster_is_isolated(monkeypatch, caplog):
    monkeypatch.setattr(_AUTH, _auth_from_arn)
    monkeypatch.setattr(
        "clont.finops.k8s.source.KubernetesSource._read_cluster",
        lambda self: (_ for _ in ()).throw(RuntimeError("connection refused")),
    )
    config = Config(
        aws={"prod": AWSConfig(role_arn="arn:aws:iam::111111111111:role/prod")},
        kubernetes={"lab": KubernetesCluster(priced_by="prod")},
    )
    with caplog.at_level("WARNING"):
        agent = build_agent(config)
    assert [p.alias for p in agent._providers] == ["prod"]
    assert "skipping kubernetes cluster lab" in caplog.text
