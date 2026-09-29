"""AWS price estimates for FinOps savings figures.

Rates come from `prices.json`, generated offline from the AWS Price List bulk
API by `tools/gen_prices.py` — on-demand, USD, Linux/shared tenancy. No runtime
call, no IAM, no network.

Rates are per instance *type*, per region, as AWS publishes them. The table used
to carry one rate per family at `.large` and scale it by the size factor — that
got 199 of 1249 us-east-1 types wrong by more than 10%, worst on the high-memory
ones: `u7in-32tb.224xlarge` is $361/hr and came out as $0.096. The scaling is
still here, but only as the fallback for a type launched after the table was
generated, and it comes back `approximate`.

Every rate has a *region*, and asking for one we don't have falls back to
us-east-1 and says so — `Quote.approximate` — so a Frankfurt volume priced at
Virginia rates can be labelled instead of quietly passing as fact.

Still estimates: commitment discounts are flat guesses (`SP_DISCOUNT_PCT`), and
io2's cheaper IOPS tiers above 32k are not modelled.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

_TABLE = json.loads((Path(__file__).parent / "prices.json").read_text())
_REGIONS: dict[str, dict] = _TABLE["regions"]
BASE_REGION: str = _TABLE["base_region"]
GENERATED_AT: str = _TABLE["generated_at"]
_BASE = _REGIONS[BASE_REGION]

# Billing hours in a month, the figure AWS itself quotes with.
HOURS_PER_MONTH = Decimal("730")
HOURS_PER_DAY = Decimal("24")


@dataclass(frozen=True, slots=True)
class Quote:
    """A rate plus where it actually came from.

    `approximate` is True whenever the caller's region isn't what we priced —
    either it didn't say, or the table has no entry for it. It is the difference
    between "$32.85/mo" and "$32.85/mo, estimated at us-east-1 rates".
    """

    amount: Decimal
    region: str
    approximate: bool


def _lookup(region: str | None, path: tuple[str, ...]) -> Quote | None:
    """Walk `path` in `region`'s table, then us-east-1's. None = neither has it."""
    for candidate in (region, BASE_REGION):
        if candidate is None:
            continue
        node = _REGIONS.get(candidate)
        for key in path:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        if isinstance(node, str):
            return Quote(Decimal(node), candidate, candidate != region)
    return None


def _rate(region: str | None, path: tuple[str, ...], default: Decimal) -> Quote:
    """`_lookup`, falling back to `default` at us-east-1 rather than failing."""
    quote = _lookup(region, path)
    return quote if quote is not None else Quote(default, BASE_REGION, True)


def _base(path: tuple[str, ...], default: str) -> Decimal:
    """A us-east-1 rate read at import, for the module-level constants."""
    return _rate(BASE_REGION, path, Decimal(default)).amount


# EBS storage, USD per GB-month, us-east-1.
_EBS_GB_MONTH = {
    vol: Decimal(rate) for vol, rate in _BASE.get("ebs_gb_month", {}).items()
}
_EBS_DEFAULT = _EBS_GB_MONTH.get("gp2", Decimal("0.10"))

# gp3 provisioned iops and throughput, USD per unit-month, us-east-1.
_EBS_IOPS_MONTH = {
    vol: Decimal(rate) for vol, rate in _BASE.get("ebs_iops_month", {}).items()
}
_EBS_THROUGHPUT_MONTH = {
    vol: Decimal(rate) for vol, rate in _BASE.get("ebs_throughput_month", {}).items()
}

# gp3 ships this much performance in the per-GB price; only the excess is billed.
GP3_FREE_IOPS = Decimal(3000)
GP3_FREE_THROUGHPUT_MBPS = Decimal(125)
# what a gp2 volume delivers, so a gp3 replacement can be quoted at parity:
# 3 iops/GiB capped at 16k, and 250 MiBps from 334 GiB up.
_GP2_IOPS_PER_GB = Decimal(3)
_GP2_IOPS_FLOOR = Decimal(100)
_GP2_IOPS_CAP = Decimal(16000)
_GP2_FAST_THROUGHPUT_FROM_GB = 334
_GP2_THROUGHPUT_MBPS = Decimal(128)
_GP2_FAST_THROUGHPUT_MBPS = Decimal(250)

# General-purpose rate for a type we don't know; never let a miss cost nothing.
_FAMILY_DEFAULT_HOURLY = Decimal(
    _BASE.get("ec2_hourly", {}).get("m5.large", "0.096")
)

# us-east-1 monthly figures. Region-aware callers should use the functions below;
# these stay for the call sites that don't know a region yet.
EIP_MONTH = _base(("eip_hourly",), "0.005") * HOURS_PER_MONTH
# an in-use public ipv4; same rate as an idle one today, its own sku since 2024
PUBLIC_IPV4_MONTH = (
    _base(("public_ipv4_hourly",), str(EIP_MONTH / HOURS_PER_MONTH)) * HOURS_PER_MONTH
)
NAT_GATEWAY_MONTH = _base(("nat_gateway_hourly",), "0.045") * HOURS_PER_MONTH
LOAD_BALANCER_MONTH = _base(("load_balancer_hourly",), "0.0225") * HOURS_PER_MONTH

# An EBS snapshot, USD per GB-month of *changed* data. Snapshots are incremental
# so true cost is below this; used as an upper-bound ballpark on the volume size.
SNAPSHOT_GB_MONTH = _base(("snapshot_gb_month",), "0.05")

# S3 storage, USD per GB-month, us-east-1. Keys are the storage classes as
# `gen_prices.py` names them; standard and rrs are the first (under-50-TB) tier.
_S3_GB_MONTH = {
    cls: Decimal(rate) for cls, rate in _BASE.get("s3_gb_month", {}).items()
}
_S3_DEFAULT = _S3_GB_MONTH.get("standard", Decimal("0.023"))
# a `StorageClass` as the s3 api spells it -> the table's key
S3_API_CLASS = {
    "STANDARD": "standard",
    "STANDARD_IA": "standard_ia",
    "ONEZONE_IA": "onezone_ia",
    "INTELLIGENT_TIERING": "intelligent_fa",  # the frequent tier: worst case, under-promise
    "GLACIER_IR": "glacier_ir",
    "GLACIER": "glacier",
    "DEEP_ARCHIVE": "deep_archive",
    "REDUCED_REDUNDANCY": "rrs",
    "EXPRESS_ONEZONE": "express_onezone",
}

# DynamoDB throughput, us-east-1. One provisioned unit delivers 3600 requests an
# hour, so provisioned capacity beats on-demand from ~29% sustained utilization up
# — which is the whole question `dynamodb.py` answers.
_DDB = _BASE.get("dynamodb", {})
_DDB_RCU_HOURLY = Decimal(_DDB.get("read_capacity_unit_hourly", "0.00013"))
_DDB_WCU_HOURLY = Decimal(_DDB.get("write_capacity_unit_hourly", "0.00065"))
# requests one provisioned capacity unit covers in an hour
DDB_UNIT_REQUESTS_PER_HOUR = Decimal(3600)

# AWS instance-size normalization factors, rebased so large == 1. Only the named
# sizes are listed; `Nxlarge` and `metal-Nxl` are computed, so a size nobody has
# launched yet still scales instead of quietly pricing as a large.
_SIZE_FACTOR = {
    "nano": Decimal("0.0625"),
    "micro": Decimal("0.125"),
    "small": Decimal("0.25"),
    "medium": Decimal("0.5"),
    "large": Decimal("1"),
    "xlarge": Decimal("2"),
    "metal": Decimal("32"),
}
_SIZE_DEFAULT = Decimal("1")
_XLARGE = re.compile(r"^(\d+)xlarge$")
_METAL = re.compile(r"^metal-(\d+)xl$")


def _size_factor(size: str) -> Decimal:
    """How many `.large`s this size is worth."""
    if size in _SIZE_FACTOR:
        return _SIZE_FACTOR[size]
    for pattern in (_XLARGE, _METAL):
        m = pattern.match(size)
        if m:
            return Decimal(m.group(1)) * 2
    return _SIZE_DEFAULT

# Discount off on-demand for a one-year, no-upfront commitment. Deliberately
# below what AWS advertises — the advice should under-promise.
SP_DISCOUNT_PCT = Decimal("0.20")
RI_DISCOUNT_PCT = Decimal("0.25")

# Only advise committing to this share of what's uncovered right now. We read a
# snapshot, not a 30-day average, so a momentary spike must not turn into a
# year-long commitment.
COMMIT_SAFETY = Decimal("0.70")


def regions() -> list[str]:
    """Every region the table prices."""
    return sorted(_REGIONS)


def instance_quote(instance_type: str, region: str | None = None) -> Quote:
    """On-demand USD/hr for an EC2 instance type — the quoted rate where we have it.

    A type the table doesn't carry (launched since it was generated) falls back to
    its family's `.large` scaled by the size factor, then to a general-purpose
    rate, so it still costs *something* rather than vanishing from the
    uncovered-spend total. Both fallbacks come back `approximate`.
    """
    exact = _lookup(region, ("ec2_hourly", instance_type))
    if exact is not None:
        return exact
    family, _, size = instance_type.partition(".")
    quote = _lookup(region, ("ec2_hourly", f"{family}.large"))
    rate = quote.amount if quote else _FAMILY_DEFAULT_HOURLY
    where = quote.region if quote else BASE_REGION
    return Quote(rate * _size_factor(size), where, True)


def _provisioned_quote(
    volume_type: str,
    region: str | None,
    iops: int | None,
    throughput_mbps: int | None,
) -> Quote:
    """What the provisioned performance of one volume costs a month, on top of storage.

    gp3 bills iops above 3000 and throughput above 125 MiBps; io1/io2 bill every
    provisioned iop. A type with no such sku (gp2, st1, sc1) bills nothing extra —
    its performance is in the per-GB price — so a missing rate is zero, not a gap.
    """
    amount = Decimal(0)
    region_used = region or BASE_REGION
    approximate = region is None
    free_iops = GP3_FREE_IOPS if volume_type == "gp3" else Decimal(0)
    charges = (
        (("ebs_iops_month", volume_type), Decimal(iops or 0) - free_iops),
        (
            ("ebs_throughput_month", volume_type),
            Decimal(throughput_mbps or 0) - GP3_FREE_THROUGHPUT_MBPS,
        ),
    )
    for path, over in charges:
        if over <= 0:
            continue
        quote = _lookup(region, path)
        if quote is None:
            continue
        amount += quote.amount * over
        region_used, approximate = quote.region, approximate or quote.approximate
    return Quote(amount, region_used, approximate)


def ebs_quote(
    volume_type: str,
    size_gb: int,
    region: str | None = None,
    iops: int | None = None,
    throughput_mbps: int | None = None,
) -> Quote:
    """Monthly cost of a volume — storage, plus provisioned iops/throughput if given."""
    quote = _rate(region, ("ebs_gb_month", volume_type), _EBS_DEFAULT)
    known = volume_type in _EBS_GB_MONTH
    extra = _provisioned_quote(volume_type, region, iops, throughput_mbps)
    return Quote(
        quote.amount * Decimal(size_gb) + extra.amount,
        quote.region,
        quote.approximate or extra.approximate or not known,
    )


def snapshot_quote(size_gb: int, region: str | None = None) -> Quote:
    """Approximate upper-bound monthly cost of a snapshot of a `size_gb` volume."""
    quote = _rate(region, ("snapshot_gb_month",), SNAPSHOT_GB_MONTH)
    return Quote(quote.amount * Decimal(size_gb), quote.region, quote.approximate)


def _s3_rate(storage_class: str, region: str | None) -> Quote:
    """USD per GB-month for one s3 storage class."""
    quote = _rate(region, ("s3_gb_month", storage_class), _S3_DEFAULT)
    known = storage_class in _S3_GB_MONTH or storage_class in _REGIONS.get(
        region or BASE_REGION, {}
    ).get("s3_gb_month", {})
    return Quote(quote.amount, quote.region, quote.approximate or not known)


def s3_storage_quote(
    size_gb: Decimal, storage_class: str = "standard", region: str | None = None
) -> Quote:
    """Monthly cost of `size_gb` sitting in one s3 storage class."""
    quote = _s3_rate(storage_class, region)
    return Quote(quote.amount * Decimal(size_gb), quote.region, quote.approximate)


def s3_transition_quote(
    size_gb: Decimal,
    to_class: str = "standard_ia",
    from_class: str = "standard",
    region: str | None = None,
) -> Quote:
    """Monthly saving from moving `size_gb` between two storage classes.

    Storage rate difference only — per-request retrieval, the lifecycle
    transition request fee and IA's 128 KB / 30-day minimums are not modelled,
    so this is the ceiling on what a transition saves, and it is only a saving
    at all if the data is genuinely cold.
    """
    src = _s3_rate(from_class, region)
    dst = _s3_rate(to_class, region)
    delta = src.amount - dst.amount
    if delta <= 0:  # a class we don't price, or not actually cheaper
        return Quote(Decimal(0), src.region, True)
    return Quote(
        delta * Decimal(size_gb), src.region, src.approximate or dst.approximate
    )


def eip_quote(region: str | None = None) -> Quote:
    """An idle/unassociated public IPv4 address, USD per month."""
    quote = _rate(region, ("eip_hourly",), EIP_MONTH / HOURS_PER_MONTH)
    return Quote(quote.amount * HOURS_PER_MONTH, quote.region, quote.approximate)


def _public_ipv4_hourly(region: str | None) -> Quote:
    """One billable public IPv4, USD/hr — the in-use sku, else the idle rate.

    Probes `region` *and* us-east-1 for the key, so a table generated before the
    in-use SKU existed falls back to `eip_hourly` instead of pricing off the
    default.
    """
    default = PUBLIC_IPV4_MONTH / HOURS_PER_MONTH
    for key in ("public_ipv4_hourly", "eip_hourly"):
        if any(key in _REGIONS.get(r, {}) for r in (region, BASE_REGION) if r):
            return _rate(region, (key,), default)
    return Quote(default, BASE_REGION, True)


def public_ipv4_quote(region: str | None = None) -> Quote:
    """One billable public IPv4 address, USD per month.

    AWS bills every public IPv4 since Feb 2024, attached or not. Priced off the
    idle-address rate until the table carries the in-use SKU — identical today,
    kept separate so they can diverge.
    """
    quote = _public_ipv4_hourly(region)
    return Quote(quote.amount * HOURS_PER_MONTH, quote.region, quote.approximate)


def public_ipv4_daily_quote(region: str | None = None) -> Quote:
    """One billable public IPv4 address, USD per day — what `collect()` stamps."""
    quote = _public_ipv4_hourly(region)
    return Quote(quote.amount * HOURS_PER_DAY, quote.region, quote.approximate)


def dynamodb_provisioned_quote(
    read_units: Decimal, write_units: Decimal, hours: Decimal, region: str | None = None
) -> Quote:
    """Cost of holding `read_units`/`write_units` of provisioned capacity for `hours`.

    Throughput only — storage is billed the same in both modes, so it cancels out
    of any capacity-mode comparison. The always-free 25 units are *not* deducted:
    that allowance is account-wide, not per table, and leaving it in keeps the
    provisioned side of the comparison pessimistic.
    """
    rcu = _rate(region, ("dynamodb", "read_capacity_unit_hourly"), _DDB_RCU_HOURLY)
    wcu = _rate(region, ("dynamodb", "write_capacity_unit_hourly"), _DDB_WCU_HOURLY)
    amount = (rcu.amount * read_units + wcu.amount * write_units) * hours
    return Quote(amount, rcu.region, rcu.approximate or wcu.approximate)


def nat_gateway_quote(region: str | None = None) -> Quote:
    """A NAT gateway's fixed hourly charge, USD per month.

    Excludes data processing, which is ~zero for an idle gateway anyway.
    """
    quote = _rate(region, ("nat_gateway_hourly",), NAT_GATEWAY_MONTH / HOURS_PER_MONTH)
    return Quote(quote.amount * HOURS_PER_MONTH, quote.region, quote.approximate)


def load_balancer_quote(region: str | None = None) -> Quote:
    """An idle ALB/NLB's hourly charge, USD per month.

    Excludes LCU charges (also ~zero with no traffic); a coarse figure across
    load balancer types.
    """
    quote = _rate(region, ("load_balancer_hourly",), LOAD_BALANCER_MONTH / HOURS_PER_MONTH)
    return Quote(quote.amount * HOURS_PER_MONTH, quote.region, quote.approximate)


# The Decimal-returning surface the collectors already use. Region-aware from
# here on, but the argument is optional so no caller had to change with the
# table; threading real regions through is a follow-up.


def instance_hourly(instance_type: str, region: str | None = None) -> Decimal:
    return instance_quote(instance_type, region).amount


def snapshot_monthly(size_gb: int, region: str | None = None) -> Decimal:
    return snapshot_quote(size_gb, region).amount


def ebs_monthly(
    volume_type: str,
    size_gb: int,
    region: str | None = None,
    iops: int | None = None,
    throughput_mbps: int | None = None,
) -> Decimal:
    return ebs_quote(volume_type, size_gb, region, iops, throughput_mbps).amount


def s3_storage_monthly(
    size_gb: Decimal, storage_class: str = "standard", region: str | None = None
) -> Decimal:
    return s3_storage_quote(size_gb, storage_class, region).amount


def gp2_performance(size_gb: int) -> tuple[int, int]:
    """What a gp2 volume of this size delivers, as (iops, MiBps).

    gp2 has nothing to provision — performance comes off the size — so this is
    what a gp3 replacement has to be provisioned *to*, and that provisioning is
    billed separately. The 3 iops/GiB baseline is above gp3's free 3000 from
    1000 GiB up, which is where the naive storage-only saving starts to lie.
    """
    iops = min(_GP2_IOPS_CAP, max(_GP2_IOPS_FLOOR, _GP2_IOPS_PER_GB * Decimal(size_gb)))
    mbps = (
        _GP2_FAST_THROUGHPUT_MBPS
        if size_gb >= _GP2_FAST_THROUGHPUT_FROM_GB
        else _GP2_THROUGHPUT_MBPS
    )
    return int(iops), int(mbps)


def ebs_gp2_to_gp3_quote(
    size_gb: int,
    region: str | None = None,
    iops: int | None = None,
    throughput_mbps: int | None = None,
) -> Quote:
    """Monthly saving from migrating a gp2 volume to gp3 at the same performance.

    Storage-rate difference *minus* the gp3 provisioned iops/throughput needed to
    match what gp2 gave for free. On a 16 TiB volume that is $70/mo of the $327
    storage saving, which the old storage-only figure quietly kept. `iops` and
    `throughput_mbps` override the size-derived baseline when the caller knows
    them; the saving is floored at zero rather than going negative.
    """
    gp2 = _rate(region, ("ebs_gb_month", "gp2"), _EBS_GB_MONTH["gp2"])
    gp3 = _rate(region, ("ebs_gb_month", "gp3"), _EBS_GB_MONTH["gp3"])
    baseline_iops, baseline_mbps = gp2_performance(size_gb)
    extra = _provisioned_quote(
        "gp3",
        region,
        iops if iops is not None else baseline_iops,
        throughput_mbps if throughput_mbps is not None else baseline_mbps,
    )
    saving = (gp2.amount - gp3.amount) * Decimal(size_gb) - extra.amount
    return Quote(
        max(Decimal(0), saving),
        gp2.region,
        gp2.approximate or gp3.approximate or extra.approximate,
    )


def ebs_gp2_to_gp3_monthly(
    size_gb: int,
    region: str | None = None,
    iops: int | None = None,
    throughput_mbps: int | None = None,
) -> Decimal:
    return ebs_gp2_to_gp3_quote(size_gb, region, iops, throughput_mbps).amount


def commitment_monthly_saving(uncovered_hourly: Decimal, discount: Decimal) -> Decimal:
    """Monthly saving from committing to the safe share of `uncovered_hourly`."""
    return uncovered_hourly * COMMIT_SAFETY * discount * HOURS_PER_MONTH


def commitment_hourly(uncovered_hourly: Decimal) -> Decimal:
    """The hourly commitment to advise for a given uncovered on-demand rate."""
    return uncovered_hourly * COMMIT_SAFETY
