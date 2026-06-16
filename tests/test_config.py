"""Config loads the alias-keyed `aws` map from YAML."""

from __future__ import annotations

import pytest

from clont.agent.bootstrap import build_agent
from clont.core.config import Config

_YAML = """\
interval_seconds: 120
aws:
  prod:
    role_arn: arn:aws:iam::111111111111:role/clont-readonly
    regions: [us-east-1, eu-west-1]
  staging:
    role_arn: arn:aws:iam::222222222222:role/clont-readonly
"""


def _load(tmp_path, monkeypatch, yaml: str) -> Config:
    cfg_file = tmp_path / "clont.yaml"
    cfg_file.write_text(yaml)
    monkeypatch.setenv("CLONT_CONFIG", str(cfg_file))
    return Config()


def test_aws_loads_as_alias_keyed_dict(tmp_path, monkeypatch):
    config = _load(tmp_path, monkeypatch, _YAML)

    assert list(config.aws) == ["prod", "staging"]
    assert config.aws["prod"].role_arn.endswith(":role/clont-readonly")
    assert config.aws["prod"].regions == ["us-east-1", "eu-west-1"]
    assert config.aws["staging"].regions == []  # default
    assert config.interval_seconds == 120


_TUNING_YAML = """\
finops:
  spend_baseline_days: 5
  idle_cpu_pct: 12
  idle_lookback_days: 30
  idle_rds_max_connections: 2
  snapshot_max_age_days: 45
monitoring:
  anomaly_sigma: 4
  anomaly_min_points: 10
  free_storage_min_pct: 15
  disk_used_max_pct: 85
  cpu_credit_min_balance: 30
  swap_usage_max_mb: 100
  disk_full_forecast_days: 7
"""


def test_finops_and_monitoring_tuning_load_from_yaml(tmp_path, monkeypatch):
    config = _load(tmp_path, monkeypatch, _TUNING_YAML)

    assert config.finops.idle_cpu_pct == 12
    assert config.finops.idle_lookback_days == 30
    assert config.finops.idle_rds_max_connections == 2
    assert config.finops.snapshot_max_age_days == 45
    assert config.monitoring.anomaly_sigma == 4
    assert config.monitoring.anomaly_min_points == 10
    assert config.monitoring.free_storage_min_pct == 15
    assert config.monitoring.disk_used_max_pct == 85
    assert config.monitoring.cpu_credit_min_balance == 30
    assert config.monitoring.swap_usage_max_mb == 100
    assert config.monitoring.disk_full_forecast_days == 7


def test_tuning_defaults_when_section_absent(tmp_path, monkeypatch):
    # No finops/monitoring sections -> documented defaults.
    config = _load(tmp_path, monkeypatch, "interval_seconds: 300\n")
    assert config.finops.idle_cpu_pct == 5.0
    assert config.finops.snapshot_max_age_days == 90
    assert config.monitoring.anomaly_sigma == 3.0
    assert config.monitoring.anomaly_min_points == 6


def test_tuning_flows_through_bootstrap_into_agent(tmp_path, monkeypatch):
    # With no `aws` accounts, build_agent needs no cloud auth — it still wires
    # the tuning + anomaly params from config into the Agent.
    config = _load(tmp_path, monkeypatch, _TUNING_YAML)
    agent = build_agent(config)

    tuning = agent._finops_tuning
    assert tuning.idle_cpu_pct == 12
    assert tuning.idle_lookback_days == 30
    assert tuning.idle_rds_max_connections == 2
    assert tuning.snapshot_max_age_days == 45

    metric_detectors = agent._monitoring_metric_detectors
    anomaly = next(d for d in metric_detectors if hasattr(d, "_sigma"))
    assert anomaly._sigma == 4
    assert anomaly._min_points == 10

    rules = next(d for d in metric_detectors if hasattr(d, "_by_metric"))
    assert rules._by_metric["FreeStoragePercent"].limit == 15
    assert rules._by_metric["SwapUsage"].limit == 100

    forecast = next(d for d in metric_detectors if hasattr(d, "_forecast_days"))
    assert forecast._forecast_days == 7


def test_unknown_finops_key_is_rejected(tmp_path, monkeypatch):
    bad = "finops:\n  idle_cpu_percentage: 5\n"  # typo'd key name
    with pytest.raises(Exception):
        _load(tmp_path, monkeypatch, bad)


_GOVERNANCE_YAML = """\
finops:
  budget_warn_pct: 75
  forecast_alpha: 0.3
  budgets:
    - account: prod
      monthly_limit: 5000
    - account: "*"
      service: Amazon Elastic Compute Cloud - Compute
      monthly_limit: 2000
      currency: USD
  ri_sp_min_utilization: 85
  ri_sp_min_coverage: 60
  nonprod_tags:
    Environment: [dev, staging]
  required_tags: [Owner, Environment]
"""


def test_budgets_and_governance_load_from_yaml(tmp_path, monkeypatch):
    from decimal import Decimal

    config = _load(tmp_path, monkeypatch, _GOVERNANCE_YAML)

    assert config.finops.budget_warn_pct == 75
    assert config.finops.forecast_alpha == 0.3
    assert [b.monthly_limit for b in config.finops.budgets] == [Decimal(5000), Decimal(2000)]
    assert config.finops.budgets[0].account == "prod"
    assert config.finops.budgets[0].service is None
    assert config.finops.budgets[1].account == "*"
    assert config.finops.budgets[1].service == "Amazon Elastic Compute Cloud - Compute"
    assert config.finops.ri_sp_min_utilization == 85
    assert config.finops.nonprod_tags == {"Environment": ["dev", "staging"]}
    assert config.finops.required_tags == ["Owner", "Environment"]


def test_governance_flows_through_bootstrap_into_agent(tmp_path, monkeypatch):
    from decimal import Decimal

    config = _load(tmp_path, monkeypatch, _GOVERNANCE_YAML)
    agent = build_agent(config)

    tuning = agent._finops_tuning
    assert tuning.ri_sp_min_utilization == 85
    assert tuning.ri_sp_min_coverage == 60
    assert tuning.nonprod_tags == {"Environment": ("dev", "staging")}
    assert tuning.required_tags == ("Owner", "Environment")

    # Budget + forecast detectors are wired into the cost-detector chain.
    budget = next(d for d in agent._finops_cost_detectors if hasattr(d, "_budgets"))
    assert [b.monthly_limit for b in budget._budgets] == [Decimal(5000), Decimal(2000)]
    assert budget._warn == Decimal("0.75")


_API_YAML = """\
api:
  url: https://api.example.com/ingest
  api_key: secret-key
  timeout_seconds: 5
"""


def test_api_block_loads_from_yaml(tmp_path, monkeypatch):
    config = _load(tmp_path, monkeypatch, _API_YAML)
    assert config.api is not None
    assert config.api.url == "https://api.example.com/ingest"
    assert config.api.api_key == "secret-key"
    assert config.api.timeout_seconds == 5


def test_api_absent_means_no_uplink(tmp_path, monkeypatch):
    config = _load(tmp_path, monkeypatch, "interval_seconds: 300\n")
    assert config.api is None
    assert build_agent(config)._uplink is None


def test_api_flows_through_bootstrap_into_agent(tmp_path, monkeypatch):
    config = _load(tmp_path, monkeypatch, _API_YAML)
    uplink = build_agent(config)._uplink
    assert uplink is not None
    assert uplink._url == "https://api.example.com/ingest"
    assert uplink._api_key == "secret-key"
    assert uplink._timeout == 5


def test_unknown_api_key_is_rejected(tmp_path, monkeypatch):
    bad = "api:\n  url: https://x\n  api_key: k\n  retries: 3\n"  # unknown field
    with pytest.raises(Exception):
        _load(tmp_path, monkeypatch, bad)


def test_api_url_must_be_https(tmp_path, monkeypatch):
    # http:// would send the Bearer api_key in cleartext -> rejected at load.
    insecure = "api:\n  url: http://api.example.com/ingest\n  api_key: k\n"
    with pytest.raises(Exception, match="https"):
        _load(tmp_path, monkeypatch, insecure)
