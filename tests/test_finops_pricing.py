"""FinOps price estimates.

`pricing` is coarse-but-load-bearing: every recommendation's dollar figure flows
through it, so we pin the rate table and the derived helpers.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from clont.finops.aws import pricing


# --- pricing helpers --------------------------------------------------------


def test_ebs_monthly_uses_type_rate():
    assert pricing.ebs_monthly("gp3", 100) == Decimal("0.08") * 100
    assert pricing.ebs_monthly("io2", 50) == Decimal("0.125") * 50


def test_ebs_monthly_unknown_type_falls_back_to_default():
    assert pricing.ebs_monthly("mystery", 10) == pricing._EBS_DEFAULT * 10


def test_ebs_gp2_to_gp3_saving_is_rate_delta_on_a_small_volume():
    # under 1000 GiB gp2's baseline iops fit in gp3's free 3000, so only the
    # 3 MiBps gp2 gives above gp3's free 125 comes off the storage delta
    size = 200
    expected = (Decimal("0.10") - Decimal("0.08")) * size - Decimal("0.04") * 3
    assert pricing.ebs_gp2_to_gp3_monthly(size) == expected
    assert pricing.ebs_gp2_to_gp3_monthly(0) == Decimal(0)


def test_ebs_iops_and_throughput_are_billed_above_the_free_tier():
    region = pricing.BASE_REGION
    storage = Decimal("0.08") * 500
    quote = pricing.ebs_quote("gp3", 500, region, iops=6000, throughput_mbps=250)
    assert quote.amount == storage + Decimal("0.005") * 3000 + Decimal("0.04") * 125
    assert not quote.approximate
    # exactly the free baseline costs nothing extra
    assert pricing.ebs_quote("gp3", 500, region, 3000, 125).amount == storage


def test_io2_bills_every_provisioned_iop():
    region = pricing.BASE_REGION
    quote = pricing.ebs_quote("io2", 100, region, iops=5000)
    assert quote.amount == Decimal("0.125") * 100 + Decimal("0.065") * 5000


def test_a_volume_type_with_no_iops_sku_bills_only_storage():
    # gp2's `Iops` is the size-derived baseline; st1/sc1 have none at all
    region = pricing.BASE_REGION
    assert pricing.ebs_quote("gp2", 1000, region, iops=3000).amount == Decimal("0.10") * 1000
    assert pricing.ebs_quote("st1", 1000, region, iops=500).amount == Decimal("0.045") * 1000


def test_free_performance_is_skipped_by_type_not_by_a_missing_rate(monkeypatch):
    # every caller sends the volume's iops, gp2's included, so a gp2 sku appearing
    # in the table must not start charging for performance that ships free
    region = pricing.BASE_REGION
    monkeypatch.setitem(pricing._REGIONS[region]["ebs_iops_month"], "gp2", "0.005")
    monkeypatch.setitem(pricing._REGIONS[region]["ebs_throughput_month"], "io2", "0.04")

    assert pricing.ebs_quote("gp2", 1000, region, iops=3000).amount == Decimal("0.10") * 1000
    # and io2 bills iops only — throughput comes with the iops, it is not a sku
    io2 = pricing.ebs_quote("io2", 100, region, iops=5000, throughput_mbps=1000)
    assert io2.amount == Decimal("0.125") * 100 + Decimal("0.065") * 5000


def test_gp2_to_gp3_subtracts_the_iops_needed_for_parity():
    size = 4000  # 12000 gp2 iops, 9000 of them billable on gp3
    storage_delta = (Decimal("0.10") - Decimal("0.08")) * size
    parity = Decimal("0.005") * 9000 + Decimal("0.04") * 125
    assert pricing.ebs_gp2_to_gp3_monthly(size, pricing.BASE_REGION) == storage_delta - parity


def test_gp2_to_gp3_never_advises_a_negative_saving():
    # a caller-provided iops number can outrun the storage delta; floor at zero
    assert pricing.ebs_gp2_to_gp3_monthly(100, pricing.BASE_REGION, iops=16000) == Decimal(0)


def test_gp2_performance_matches_what_aws_delivers():
    assert pricing.gp2_performance(10) == (100, 128)  # the 100 iops floor
    assert pricing.gp2_performance(500) == (1500, 250)  # 3 iops/GiB, fast throughput
    assert pricing.gp2_performance(20000) == (16000, 250)  # capped at 16k


def test_snapshot_monthly_scales_with_size():
    assert pricing.snapshot_monthly(40) == Decimal("0.05") * 40


def test_pricing_returns_decimal_not_float():
    # Money is Decimal end-to-end; a float here would poison downstream sums.
    assert isinstance(pricing.ebs_monthly("gp3", 10), Decimal)
    assert isinstance(pricing.snapshot_monthly(10), Decimal)
    assert isinstance(pricing.EIP_MONTH, Decimal)
    assert isinstance(pricing.NAT_GATEWAY_MONTH, Decimal)
    assert isinstance(pricing.LOAD_BALANCER_MONTH, Decimal)


# --- ec2 instance rates -----------------------------------------------------


def test_instance_hourly_is_the_quoted_rate_per_type():
    assert pricing.instance_hourly("m5.large") == Decimal("0.096")
    assert pricing.instance_hourly("m5.4xlarge") == Decimal("0.768")
    for itype in ("m5.large", "m5.4xlarge", "c5.metal"):
        assert not pricing.instance_quote(itype, pricing.BASE_REGION).approximate


def test_a_high_memory_type_is_not_its_family_scaled():
    # the reason the table is per type: 32 TB of ram is not 448 larges
    quote = pricing.instance_quote("u7in-32tb.224xlarge", pricing.BASE_REGION)
    assert not quote.approximate
    assert quote.amount > Decimal(300)
    # the family table carried no `.large` for it, so scaling priced 32 TB at cents
    scaled = pricing._FAMILY_DEFAULT_HOURLY * pricing._size_factor("224xlarge")
    assert quote.amount > scaled * 5


def test_a_size_aws_does_not_sell_scales_off_the_family():
    # nobody can launch m5.medium, so it can only ever be a scaled guess
    quote = pricing.instance_quote("m5.medium", pricing.BASE_REGION)
    assert quote.amount == Decimal("0.096") / 2
    assert quote.approximate


def test_instance_hourly_unknown_family_falls_back():
    # A family we've never priced still costs something, scaled by its size.
    assert pricing.instance_hourly("zz9.2xlarge") == pricing._FAMILY_DEFAULT_HOURLY * 4


def test_unlisted_sizes_still_scale():
    # a size launched after the table was generated, and its metal spelling
    assert pricing._size_factor("7xlarge") == Decimal(14)
    assert pricing._size_factor("192xlarge") == Decimal(384)
    assert pricing._size_factor("metal-48xl") == Decimal(96)
    assert pricing._size_factor("mystery") == Decimal(1)


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


def test_every_region_prices_instances_per_type():
    # a thin row reads as a real table and quietly scales everything off m5
    for region in pricing.regions():
        types = pricing._REGIONS[region].get("ec2_hourly", {})
        assert len(types) > 100, f"{region} carries {len(types)} instance types"


def test_every_region_prices_the_gp3_performance_it_sells():
    # storage without the iops sku understates every gp2 -> gp3 saving
    for region in pricing.regions():
        row = pricing._REGIONS[region]
        if "gp3" not in row.get("ebs_gb_month", {}):
            continue
        assert "gp3" in row.get("ebs_iops_month", {}), f"{region} has no gp3 iops rate"
        assert "gp3" in row.get("ebs_throughput_month", {}), f"{region} has no gp3 mbps rate"


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


# --- s3 storage classes -----------------------------------------------------


def test_s3_storage_monthly_uses_the_class_rate():
    assert pricing.s3_storage_monthly(Decimal(100), "standard") == Decimal("0.023") * 100
    assert pricing.s3_storage_monthly(Decimal(100), "standard_ia") == Decimal("0.0125") * 100


def test_s3_transition_saving_is_the_rate_delta():
    size = Decimal(1000)
    expected = (Decimal("0.023") - Decimal("0.0125")) * size
    quote = pricing.s3_transition_quote(size, "standard_ia", "standard", "us-east-1")
    assert quote.amount == expected
    assert not quote.approximate
    # never more than just deleting the data
    assert quote.amount < pricing.s3_storage_monthly(size, "standard", "us-east-1")


def test_s3_transition_to_a_class_we_do_not_price_invents_nothing():
    # deep archive has no byte-hrs sku in the s3 offer; a fallback rate here
    # would quote a saving off the standard rate, i.e. zero dollars as a number
    quote = pricing.s3_transition_quote(Decimal(1000), "mystery", "standard", "us-east-1")
    assert quote.amount == Decimal(0)
    assert quote.approximate


def test_s3_transition_the_wrong_way_round_is_not_a_saving():
    assert pricing.s3_transition_quote(
        Decimal(1000), "standard", "glacier", "us-east-1"
    ).amount == Decimal(0)


def test_every_region_prices_the_classes_the_collector_reads():
    for region in pricing.regions():
        for cls in ("standard", "standard_ia"):
            quote = pricing.s3_storage_quote(Decimal(1), cls, region)
            assert quote.region == region, f"{region}/{cls} fell back"
            assert not quote.approximate
            assert quote.amount > 0
