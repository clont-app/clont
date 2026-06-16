"""Coarse AWS price estimates for FinOps savings figures.

These are **approximate** on-demand, us-east-1, USD rates — enough to put a
ballpark dollar value on a recommendation, not billing-accurate (real cost
varies by region, commitment discounts, and provisioned IOPS/throughput). Kept
deliberately small; refine per-region later if it becomes worth it.
"""

from __future__ import annotations

from decimal import Decimal

# EBS storage, USD per GB-month.
_EBS_GB_MONTH = {
    "gp3": Decimal("0.08"),
    "gp2": Decimal("0.10"),
    "io1": Decimal("0.125"),
    "io2": Decimal("0.125"),
    "st1": Decimal("0.045"),
    "sc1": Decimal("0.015"),
    "standard": Decimal("0.05"),
}
_EBS_DEFAULT = Decimal("0.10")

# An idle/unassociated public IPv4 address, USD per month (~$0.005/hr * 730).
EIP_MONTH = Decimal("3.60")

# A NAT gateway's fixed hourly charge, USD per month (~$0.045/hr * 730). Excludes
# data-processing charges, which are ~zero for an idle gateway anyway.
NAT_GATEWAY_MONTH = Decimal("32.85")

# An idle ALB/NLB's hourly charge, USD per month (~$0.0225/hr * 730). Excludes
# LCU charges (also ~zero with no traffic). A coarse figure across LB types.
LOAD_BALANCER_MONTH = Decimal("16.43")

# An EBS snapshot, USD per GB-month of *changed* data. Snapshots are incremental
# so true cost is below this; used as an upper-bound ballpark on the volume size.
SNAPSHOT_GB_MONTH = Decimal("0.05")


def snapshot_monthly(size_gb: int) -> Decimal:
    """Approximate upper-bound monthly cost of a snapshot of a `size_gb` volume."""
    return SNAPSHOT_GB_MONTH * Decimal(size_gb)


def ebs_monthly(volume_type: str, size_gb: int) -> Decimal:
    """Approximate monthly storage cost of a volume."""
    return _EBS_GB_MONTH.get(volume_type, _EBS_DEFAULT) * Decimal(size_gb)


def ebs_gp2_to_gp3_monthly(size_gb: int) -> Decimal:
    """Approximate monthly saving from migrating a gp2 volume to gp3.

    Storage-rate difference only. It ignores gp3's separately-billed provisioned
    IOPS/throughput above the free baseline, so for very large or high-IOPS
    volumes the real saving can be smaller — this is a ballpark, not a quote.
    """
    return (_EBS_GB_MONTH["gp2"] - _EBS_GB_MONTH["gp3"]) * Decimal(size_gb)
