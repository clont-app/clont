"""FinOps price estimates + the not-yet-implemented S3/EC2 collector contracts.

`pricing` is coarse-but-load-bearing: every recommendation's dollar figure flows
through it, so we pin the rate table and the derived helpers. The S3/EC2
collectors are registered stubs; we lock in that they register correctly and
still signal `NotImplementedError` so a half-wired collector can't silently
report zero savings.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from clont.core import registry
from clont.core.models import Cloud, CloudResource, Period
from clont.finops.aws import ec2, pricing, s3


# --- pricing helpers --------------------------------------------------------


def test_ebs_monthly_uses_type_rate():
    assert pricing.ebs_monthly("gp3", 100) == Decimal("0.08") * 100
    assert pricing.ebs_monthly("io2", 50) == Decimal("0.125") * 50


def test_ebs_monthly_unknown_type_falls_back_to_default():
    assert pricing.ebs_monthly("mystery", 10) == pricing._EBS_DEFAULT * 10


def test_ebs_gp2_to_gp3_saving_is_rate_delta():
    size = 200
    expected = (Decimal("0.10") - Decimal("0.08")) * size
    assert pricing.ebs_gp2_to_gp3_monthly(size) == expected
    assert pricing.ebs_gp2_to_gp3_monthly(0) == Decimal(0)


def test_snapshot_monthly_scales_with_size():
    assert pricing.snapshot_monthly(40) == Decimal("0.05") * 40


def test_pricing_returns_decimal_not_float():
    # Money is Decimal end-to-end; a float here would poison downstream sums.
    assert isinstance(pricing.ebs_monthly("gp3", 10), Decimal)
    assert isinstance(pricing.snapshot_monthly(10), Decimal)
    assert isinstance(pricing.EIP_MONTH, Decimal)
    assert isinstance(pricing.NAT_GATEWAY_MONTH, Decimal)
    assert isinstance(pricing.LOAD_BALANCER_MONTH, Decimal)


# --- S3 / EC2 collector stubs ----------------------------------------------


class _Provider:
    alias = "prod"

    def regions(self) -> list[str]:
        return ["us-east-1"]

    def client(self, service: str, region: str | None = None):
        raise AssertionError("stub collectors must not touch a client yet")


def _period() -> Period:
    from datetime import date

    return Period(date(2026, 1, 1), date(2026, 1, 31))


@pytest.mark.parametrize(
    ("service", "cls"),
    [("s3", s3.S3CostCollector), ("ec2", ec2.EC2CostCollector)],
)
def test_stub_collectors_are_registered(service, cls):
    assert registry.get("finops", Cloud.AWS, service) is cls
    assert cls.cloud == Cloud.AWS
    assert cls.service == service


@pytest.mark.parametrize("cls", [s3.S3CostCollector, ec2.EC2CostCollector])
def test_stub_collectors_signal_not_implemented(cls):
    collector = cls(_Provider())
    with pytest.raises(NotImplementedError):
        collector.collect(_period())
    with pytest.raises(NotImplementedError):
        collector.recommendations(_period())


def test_cloudresource_typing_kept_importable():
    # Sanity: the stubs' return types are importable value objects.
    r = CloudResource(Cloud.AWS, "s3", "my-bucket")
    assert r.resource_id == "my-bucket"
