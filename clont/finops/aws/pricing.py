"""AWS price estimates for FinOps savings figures.

Rates come from `prices.json`, generated offline from the AWS Price List bulk
API by `tools/gen_prices.py` — on-demand, USD, Linux/shared tenancy. No runtime
call, no IAM, no network.

Still estimates, deliberately: one rate per instance family at `.large` scaled
by the size factor, so a `.24xlarge` is 48 larges rather than its own quoted
rate. Commitment discounts and provisioned IOPS/throughput are not modelled.

What changed is the honesty. Every rate now has a *region*, and asking for one
we don't have falls back to us-east-1 and says so — `Quote.approximate` — so a
Frankfurt volume priced at Virginia rates can be labelled instead of quietly
passing as fact.
"""

from __future__ import annotations

import json
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


def _rate(region: str | None, path: tuple[str, ...], default: Decimal) -> Quote:
    """Walk `path` in `region`'s table, falling back to us-east-1, then `default`."""
    for candidate, approximate in ((region, False), (BASE_REGION, True)):
        if candidate is None:
            continue
        node = _REGIONS.get(candidate)
        for key in path:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        if isinstance(node, str):
            return Quote(Decimal(node), candidate, approximate or region != candidate)
    return Quote(default, BASE_REGION, True)


def _base(path: tuple[str, ...], default: str) -> Decimal:
    """A us-east-1 rate read at import, for the module-level constants."""
    return _rate(BASE_REGION, path, Decimal(default)).amount


# EBS storage, USD per GB-month, us-east-1.
_EBS_GB_MONTH = {
    vol: Decimal(rate) for vol, rate in _BASE.get("ebs_gb_month", {}).items()
}
_EBS_DEFAULT = _EBS_GB_MONTH.get("gp2", Decimal("0.10"))

# EC2 on-demand, us-east-1, linux, USD/hr for the ``.large`` size of each family.
_FAMILY_LARGE_HOURLY = {
    fam: Decimal(rate) for fam, rate in _BASE.get("ec2_family_large_hourly", {}).items()
}
# General-purpose rate for a family we don't know; never let a miss cost nothing.
_FAMILY_DEFAULT_HOURLY = _FAMILY_LARGE_HOURLY.get("m5", Decimal("0.096"))

# us-east-1 monthly figures. Region-aware callers should use the functions below;
# these stay for the call sites that don't know a region yet.
EIP_MONTH = _base(("eip_hourly",), "0.005") * HOURS_PER_MONTH
NAT_GATEWAY_MONTH = _base(("nat_gateway_hourly",), "0.045") * HOURS_PER_MONTH
LOAD_BALANCER_MONTH = _base(("load_balancer_hourly",), "0.0225") * HOURS_PER_MONTH

# An EBS snapshot, USD per GB-month of *changed* data. Snapshots are incremental
# so true cost is below this; used as an upper-bound ballpark on the volume size.
SNAPSHOT_GB_MONTH = _base(("snapshot_gb_month",), "0.05")

# AWS instance-size normalization factors, rebased so large == 1.
_SIZE_FACTOR = {
    "nano": Decimal("0.0625"),
    "micro": Decimal("0.125"),
    "small": Decimal("0.25"),
    "medium": Decimal("0.5"),
    "large": Decimal("1"),
    "xlarge": Decimal("2"),
    "2xlarge": Decimal("4"),
    "3xlarge": Decimal("6"),
    "4xlarge": Decimal("8"),
    "6xlarge": Decimal("12"),
    "8xlarge": Decimal("16"),
    "9xlarge": Decimal("18"),
    "10xlarge": Decimal("20"),
    "12xlarge": Decimal("24"),
    "16xlarge": Decimal("32"),
    "18xlarge": Decimal("36"),
    "24xlarge": Decimal("48"),
    "32xlarge": Decimal("64"),
    "48xlarge": Decimal("96"),
    "metal": Decimal("32"),
}
_SIZE_DEFAULT = Decimal("1")

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
    """Approximate on-demand USD/hr for an EC2 instance type.

    Unknown families fall back to a general-purpose rate scaled by size, so a
    type we've never seen still carries a non-zero cost rather than vanishing
    from the uncovered-spend total.
    """
    family, _, size = instance_type.partition(".")
    quote = _rate(region, ("ec2_family_large_hourly", family), _FAMILY_DEFAULT_HOURLY)
    factor = _SIZE_FACTOR.get(size, _SIZE_DEFAULT)
    known = family in _FAMILY_LARGE_HOURLY or family in _REGIONS.get(
        region or BASE_REGION, {}
    ).get("ec2_family_large_hourly", {})
    return Quote(quote.amount * factor, quote.region, quote.approximate or not known)


def ebs_quote(volume_type: str, size_gb: int, region: str | None = None) -> Quote:
    """Approximate monthly storage cost of a volume."""
    quote = _rate(region, ("ebs_gb_month", volume_type), _EBS_DEFAULT)
    known = volume_type in _EBS_GB_MONTH
    return Quote(quote.amount * Decimal(size_gb), quote.region, quote.approximate or not known)


def snapshot_quote(size_gb: int, region: str | None = None) -> Quote:
    """Approximate upper-bound monthly cost of a snapshot of a `size_gb` volume."""
    quote = _rate(region, ("snapshot_gb_month",), SNAPSHOT_GB_MONTH)
    return Quote(quote.amount * Decimal(size_gb), quote.region, quote.approximate)


def eip_quote(region: str | None = None) -> Quote:
    """An idle/unassociated public IPv4 address, USD per month."""
    quote = _rate(region, ("eip_hourly",), EIP_MONTH / HOURS_PER_MONTH)
    return Quote(quote.amount * HOURS_PER_MONTH, quote.region, quote.approximate)


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


def ebs_monthly(volume_type: str, size_gb: int, region: str | None = None) -> Decimal:
    return ebs_quote(volume_type, size_gb, region).amount


def ebs_gp2_to_gp3_monthly(size_gb: int, region: str | None = None) -> Decimal:
    """Approximate monthly saving from migrating a gp2 volume to gp3.

    Storage-rate difference only. It ignores gp3's separately-billed provisioned
    IOPS/throughput above the free baseline, so for very large or high-IOPS
    volumes the real saving can be smaller — this is a ballpark, not a quote.
    """
    gp2 = _rate(region, ("ebs_gb_month", "gp2"), _EBS_GB_MONTH["gp2"]).amount
    gp3 = _rate(region, ("ebs_gb_month", "gp3"), _EBS_GB_MONTH["gp3"]).amount
    return (gp2 - gp3) * Decimal(size_gb)


def commitment_monthly_saving(uncovered_hourly: Decimal, discount: Decimal) -> Decimal:
    """Monthly saving from committing to the safe share of `uncovered_hourly`."""
    return uncovered_hourly * COMMIT_SAFETY * discount * HOURS_PER_MONTH


def commitment_hourly(uncovered_hourly: Decimal) -> Decimal:
    """The hourly commitment to advise for a given uncovered on-demand rate."""
    return uncovered_hourly * COMMIT_SAFETY
