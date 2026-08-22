"""Provider auth + region discovery, mocked with moto."""

from __future__ import annotations

import pytest
from moto import mock_aws

from clont.core.config import AWSConfig, CURConfig
from clont.providers.aws.provider import AWSProvider

ROLE_ARN = "arn:aws:iam::123456789012:role/clont-readonly"


@pytest.fixture
def aws_creds(monkeypatch):
    """Dummy base credentials so the assume-role source chain resolves."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@mock_aws
def test_authenticate_builds_usable_session(aws_creds):
    provider = AWSProvider("prod", AWSConfig(role_arn=ROLE_ARN))
    provider.authenticate()

    ident = provider.client("sts").get_caller_identity()
    assert ident["Account"]
    assert provider.alias == "prod"
    assert provider.account_id == ident["Account"]  # populated from get_caller_identity


@mock_aws
def test_regions_uses_configured_list(aws_creds):
    provider = AWSProvider(
        "prod", AWSConfig(role_arn=ROLE_ARN, regions=["eu-west-1", "us-east-2"])
    )
    provider.authenticate()

    assert provider.regions() == ["eu-west-1", "us-east-2"]


@mock_aws
def test_regions_falls_back_to_describe_regions(aws_creds):
    provider = AWSProvider("prod", AWSConfig(role_arn=ROLE_ARN))  # no regions configured
    provider.authenticate()

    regions = provider.regions()
    assert "us-east-1" in regions
    assert len(regions) > 1


@mock_aws
def test_client_is_cached(aws_creds):
    provider = AWSProvider("prod", AWSConfig(role_arn=ROLE_ARN))
    provider.authenticate()

    first = provider.client("ec2", "us-east-1")
    second = provider.client("ec2", "us-east-1")
    assert first is second


def test_client_before_authenticate_raises():
    provider = AWSProvider("prod", AWSConfig(role_arn=ROLE_ARN))
    with pytest.raises(RuntimeError):
        provider.client("ec2")


@mock_aws
def test_preflight_never_touches_cost_explorer(aws_creds):
    provider = AWSProvider("prod", AWSConfig(role_arn=ROLE_ARN))
    provider.authenticate()
    calls: list[str] = []
    real = provider.client

    def recording(service: str, region: str | None = None):
        calls.append(service)
        return real(service, region)

    provider.client = recording  # type: ignore[method-assign]
    provider._probe_savings_plans = lambda: None  # moto has no savingsplans
    provider._probe_compute_optimizer = lambda: None  # nor compute-optimizer
    provider.preflight()

    assert "ce" not in calls


@mock_aws
def test_preflight_probes_the_cur_when_configured(aws_creds):
    import boto3

    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="bill")
    config = AWSConfig(
        role_arn=ROLE_ARN,
        cur=CURConfig(bucket="bill", report_name="rep", prefix="p"),
    )
    provider = AWSProvider("prod", config)
    provider.authenticate()
    provider._probe_savings_plans = lambda: None  # moto has no savingsplans
    provider._probe_compute_optimizer = lambda: None  # nor compute-optimizer

    # nothing delivered yet: readable bucket, no manifest -> not a missing grant
    assert "s3:GetObject" not in provider.preflight()
    assert provider.cur is config.cur
