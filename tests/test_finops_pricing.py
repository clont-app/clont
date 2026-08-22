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


# --- ec2 instance rates -----------------------------------------------------


def test_instance_hourly_known_family_scales_by_size():
    assert pricing.instance_hourly("m5.large") == Decimal("0.096")
    assert pricing.instance_hourly("m5.4xlarge") == Decimal("0.096") * 8
    assert pricing.instance_hourly("m5.medium") == Decimal("0.096") / 2


def test_instance_hourly_unknown_family_falls_back():
    # A family we've never priced still costs something, scaled by its size.
    assert pricing.instance_hourly("zz9.2xlarge") == pricing._FAMILY_DEFAULT_HOURLY * 4


def test_instance_hourly_never_returns_zero():
    # A zero here would silently erase an instance from the uncovered-spend total.
    for itype in ("m5.large", "zz9.mystery", "garbage", "", "c5"):
        assert pricing.instance_hourly(itype) > 0


def test_commitment_helpers_under_commit():
    hourly = Decimal("10")
    assert pricing.commitment_hourly(hourly) == hourly * pricing.COMMIT_SAFETY
    saving = pricing.commitment_monthly_saving(hourly, pricing.SP_DISCOUNT_PCT)
    assert saving == hourly * pricing.COMMIT_SAFETY * pricing.SP_DISCOUNT_PCT * Decimal("730")
    assert saving < hourly * pricing.HOURS_PER_MONTH


def test_commitment_discounts_are_conservative():
    assert Decimal(0) < pricing.SP_DISCOUNT_PCT < pricing.RI_DISCOUNT_PCT < Decimal("0.5")
    assert Decimal(0) < pricing.COMMIT_SAFETY < Decimal(1)


# --- the region axis --------------------------------------------------------


def test_every_region_in_the_table_resolves():
    # a region present but empty would silently price everything at us-east-1
    for region in pricing.regions():
        quote = pricing.ebs_quote("gp3", 100, region)
        assert quote.region == region, f"{region} fell back"
        assert not quote.approximate
        assert quote.amount > 0


def test_a_region_we_price_differs_from_virginia_somewhere():
    # the whole point of the table: sao paulo is not northern virginia
    if "sa-east-1" not in pricing.regions():
        pytest.skip("sa-east-1 not in the table")
    assert pricing.ebs_quote("gp3", 100, "sa-east-1").amount != pricing.ebs_quote(
        "gp3", 100, "us-east-1"
    ).amount


def test_unknown_region_falls_back_and_says_so():
    quote = pricing.instance_quote("m5.large", "mars-west-1")
    assert quote.region == pricing.BASE_REGION
    assert quote.approximate
    assert quote.amount == pricing.instance_quote("m5.large", pricing.BASE_REGION).amount


def test_no_region_at_all_is_also_approximate():
    # the old callers pass nothing; the number is a us-east-1 guess, not a quote
    assert pricing.nat_gateway_quote().approximate
    assert pricing.nat_gateway_quote(pricing.BASE_REGION).approximate is False


def test_an_unpriced_instance_family_is_marked_approximate():
    known = pricing.instance_quote("m5.large", pricing.BASE_REGION)
    assert not known.approximate
    assert pricing.instance_quote("zz9.large", pricing.BASE_REGION).approximate


def test_no_rate_in_the_table_is_zero_or_negative():
    # a zero erases the resource from every savings total without an error
    for region in pricing.regions():
        row = pricing._REGIONS[region]
        for key, value in row.items():
            rates = value.values() if isinstance(value, dict) else [value]
            for rate in rates:
                assert Decimal(rate) > 0, f"{region}/{key} = {rate}"


def test_the_table_carries_provenance():
    assert pricing.GENERATED_AT.endswith("Z")
    assert pricing.BASE_REGION in pricing.regions()


def test_spot_check_published_us_east_1_rates():
    # published on-demand list prices; if these drift the table is stale
    assert pricing.ebs_quote("gp3", 1, "us-east-1").amount == Decimal("0.08")
    assert pricing.instance_quote("m5.large", "us-east-1").amount == Decimal("0.096")
    assert pricing.nat_gateway_quote("us-east-1").amount == Decimal("0.045") * Decimal("730")
