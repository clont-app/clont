#!/usr/bin/env python3
"""Regenerate clont/finops/aws/prices.json from the AWS Price List bulk API.

Offline, run by hand at release time — it is not shipped in the wheel and clont
never calls it at runtime. The bulk API is free, needs no credentials, no IAM.

    python tools/gen_prices.py                 # every region
    python tools/gen_prices.py us-east-1 eu-west-1
    python tools/gen_prices.py --s3            # only the s3 rates, merged in
    python tools/gen_prices.py --dynamodb      # only the dynamodb rates

The EC2 region shards are ~480 MB of pretty-printed JSON each, so they are
scanned line by line and never held in memory — `json.load` on one wants more
RAM than most machines will give it. Products come before terms in the file, so
a single sequential pass collects the SKUs it wants, then their rates.

EC2 rates are per *instance type*, not per family: AWS prices are not linear in
size (`u7in-32tb.224xlarge` is $361/hr, `g6f.xlarge` is 41% under its family's
`.large` x2, c4 is a shade under). A family table scaled by the size factor got
199 of 1249 us-east-1 types wrong by more than 10%.

Everything but the load balancer lives in the AmazonEC2 offer (NAT gateway and
EBS included); only the ALB hourly comes from AWSELB. AmazonVPC carries the idle
public IPv4 charge. S3 storage is its own offer, and its shards are ~0.5 MB —
`--s3` refreshes just those rates in place, because a full run pulls ~17 GB of
ec2 shards to change one key. It only touches regions the table already has: a
region with s3 rates and no ec2 rates would price every instance at us-east-1
without saying so.

Deep Archive is the exception: the AmazonS3 offer has no `TimedStorage-GDA-ByteHrs`
(only `GDA-Staging`, which is staging overhead at 20x the rate), the real one sits
in the separate AmazonS3GlacierDeepArchive offer — and there the products carry no
`productFamily`, so it needs its own classifier.

Provisioned IOPS and gp3 throughput are their own skus (`System Operation` /
`Provisioned Throughput`). Throughput is quoted per **GiBps-month**, so it is
divided by 1024 to land on the per-MiBps rate everything else speaks.

Tiered rates (s3 standard, rrs) keep the first *charged* tier — the small-volume
rate, which is what a bucket under 50 TB actually pays. A leading `0.00` tier is
never a rate: dynamodb's capacity skus lead with the always-free 25 units, and
taking that tier would price provisioned capacity at nothing.

DynamoDB lives in its own offer too, and the four throughput skus are told apart
by the `group` attribute, not the usagetype: `IA-ReadRequestUnits` (the IA table
class) and `ReplWriteCapacityUnit-Hrs` (global tables) both look like the plain
sku at the end of the string, and `group` is the only field that separates them.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

BULK = "https://pricing.us-east-1.amazonaws.com"
OUT = Path(__file__).resolve().parent.parent / "clont" / "finops" / "aws" / "prices.json"

_INSTANCE_TYPE = re.compile(r"^[a-z0-9\-]+\.[a-z0-9\-]+$")
_PRODUCT_START = re.compile(r'^ {4}"([A-Z0-9]{10,20})" : \{')
_TERM_SKU = re.compile(r'^ {6}"([A-Z0-9]{10,20})" : \{')
_USD = re.compile(r'^\s*"USD" : "([0-9.]+)"')
_UNIT = re.compile(r'^\s*"unit" : "([^"]*)"')

# a rate quoted per GiBps-month is 1024 of the per-MiBps rate we store
_UNIT_DIVISOR = {"GiBps-mo": 1024}

_EBS_TYPES = {"gp3", "gp2", "io1", "io2", "st1", "sc1", "standard"}

# `.metal` sits in its own product family; same attribute filters otherwise
_COMPUTE_FAMILIES = {"Compute Instance", "Compute Instance (bare metal)"}

# provisioned iops skus, keyed by the usagetype suffix. io2's tier2/tier3 are
# cheaper per iops above 32k/64k; keeping tier1 over-states the cost, which
# under-states every saving that subtracts it.
_IOPS_SKUS = {
    "EBS:VolumeP-IOPS.gp3": "gp3",
    "EBS:VolumeP-IOPS.piops": "io1",
    "EBS:VolumeP-IOPS.io2": "io2",
}

# s3 storage classes, keyed by usagetype so the staging/overhead skus (which
# carry the same volumeType as real storage) can't be mistaken for a rate
_S3_CLASSES = {
    "TimedStorage-ByteHrs": "standard",
    "TimedStorage-SIA-ByteHrs": "standard_ia",
    "TimedStorage-ZIA-ByteHrs": "onezone_ia",
    "TimedStorage-INT-FA-ByteHrs": "intelligent_fa",
    "TimedStorage-INT-IA-ByteHrs": "intelligent_ia",
    "TimedStorage-INT-AIA-ByteHrs": "intelligent_aia",
    "TimedStorage-INT-AA-ByteHrs": "intelligent_aa",
    "TimedStorage-INT-DAA-ByteHrs": "intelligent_daa",
    "TimedStorage-GIR-ByteHrs": "glacier_ir",
    "TimedStorage-GlacierByteHrs": "glacier",
    "TimedStorage-RRS-ByteHrs": "rrs",
    "TimedStorage-XZ-ByteHrs": "express_onezone",
}
# deep archive lives in its own offer, keyed by the usagetype the AmazonS3 offer
# doesn't have. EarlyDelete-GDA carries the same rate and is not storage.
_S3_GDA_OFFER = "AmazonS3GlacierDeepArchive"
_S3_GDA_USAGETYPE = "TimedStorage-GDA-ByteHrs"
# outside us-east-1 the usagetype carries a region code (EUC1-, APS3-). the
# prefix is uppercase, which is what keeps Files-/Annotation- out.
_S3_USAGETYPE = re.compile(r"^(?:[A-Z]{2,5}[0-9]?-)?(TimedStorage-[A-Za-z0-9-]+)$")

# dynamodb throughput, keyed by (group, usagetype suffix) -> our name. the group
# is what keeps the IA table class and global-table replicated writes out.
_DDB_OFFER = "AmazonDynamoDB"
_DDB_SKUS = {
    ("DDB-ReadUnits", "ReadCapacityUnit-Hrs"): "read_capacity_unit_hourly",
    ("DDB-WriteUnits", "WriteCapacityUnit-Hrs"): "write_capacity_unit_hourly",
    ("DDB-ReadUnits", "ReadRequestUnits"): "read_request_unit",
    ("DDB-WriteUnits", "WriteRequestUnits"): "write_request_unit",
}


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=180) as resp:
        return json.load(resp)


def _region_urls(offer: str) -> dict[str, str]:
    index = _get_json(f"{BULK}/offers/v1.0/aws/{offer}/current/region_index.json")
    return {r: BULK + v["currentVersionUrl"] for r, v in index["regions"].items()}


def _attr(block: str, key: str) -> str:
    m = re.search(rf'"{key}" : "([^"]*)"', block)
    return m.group(1) if m else ""


def _classify(block: str) -> tuple[str, str] | None:
    """Which rate this product block is, as (bucket, key). None = don't care."""
    family = _attr(block, "productFamily")
    usagetype = _attr(block, "usagetype")
    if _attr(block, "locationType") != "AWS Region":
        return None  # Outposts / Local Zones / Wavelength are not the region rate
    if family in _COMPUTE_FAMILIES:
        if (
            _attr(block, "operatingSystem") != "Linux"
            or _attr(block, "tenancy") != "Shared"
            or _attr(block, "preInstalledSw") != "NA"
            or _attr(block, "capacitystatus") != "Used"
            or _attr(block, "licenseModel") != "No License required"
        ):
            return None
        itype = _attr(block, "instanceType")
        return ("ec2_hourly", itype) if _INSTANCE_TYPE.match(itype) else None
    if family == "Storage":
        vol = _attr(block, "volumeApiName")
        return ("ebs_gb_month", vol) if vol in _EBS_TYPES else None
    if family == "System Operation":
        for suffix, vol in _IOPS_SKUS.items():
            if usagetype.endswith(suffix):  # tier2/tier3 end past the type, so they miss
                return ("ebs_iops_month", vol)
        return None
    if family == "Provisioned Throughput" and usagetype.endswith("EBS:VolumeP-Throughput.gp3"):
        return ("ebs_throughput_month", "gp3")
    if family == "Storage Snapshot" and usagetype.endswith("EBS:SnapshotUsage"):
        return ("flat", "snapshot_gb_month")
    if family == "NAT Gateway" and usagetype.endswith("NatGateway-Hours"):
        return ("flat", "nat_gateway_hourly")
    if usagetype.endswith("PublicIPv4:IdleAddress"):
        return ("flat", "eip_hourly")
    # in-use addresses are a separate sku at the same rate; keep them apart
    if usagetype.endswith("PublicIPv4:InUseAddress"):
        return ("flat", "public_ipv4_hourly")
    # the plain ALB hourly - not Outposts-, not TS- (Local Zones)
    if family == "Load Balancer-Application" and usagetype.endswith("LoadBalancerUsage"):
        if "Outposts-" in usagetype or "TS-" in usagetype:
            return None
        return ("flat", "load_balancer_hourly")
    return None


def _classify_s3(block: str) -> tuple[str, str] | None:
    """Same contract as `_classify`, for the AmazonS3 offer."""
    if _attr(block, "productFamily") != "Storage":
        return None
    if _attr(block, "locationType") != "AWS Region":
        return None
    m = _S3_USAGETYPE.match(_attr(block, "usagetype"))
    if m is None:
        return None
    name = _S3_CLASSES.get(m.group(1))
    return ("s3_gb_month", name) if name else None


def _classify_ddb(block: str) -> tuple[str, str] | None:
    """Same contract as `_classify`, for the AmazonDynamoDB offer."""
    if _attr(block, "locationType") != "AWS Region":
        return None
    group = _attr(block, "group")
    usagetype = _attr(block, "usagetype")
    for (want_group, suffix), key in _DDB_SKUS.items():
        if group == want_group and usagetype.endswith(suffix):
            return ("dynamodb", key)
    return None


def _classify_s3_gda(block: str) -> tuple[str, str] | None:
    """Deep archive storage. Its offer leaves productFamily out, so don't ask."""
    if _attr(block, "locationType") != "AWS Region":
        return None
    m = _S3_USAGETYPE.match(_attr(block, "usagetype"))
    if m is None or m.group(1) != _S3_GDA_USAGETYPE:
        return None
    return ("s3_gb_month", "deep_archive")


def _trim(rate: str) -> str:
    """0.0960000000 -> 0.096. the table is read by humans in review."""
    return rate.rstrip("0").rstrip(".") if "." in rate else rate


def _scan(url: str, into: dict, classify=_classify) -> None:
    """Stream one region shard, folding the rates we recognise into `into`."""
    wanted: dict[str, tuple[str, str]] = {}
    block: list[str] = []
    sku = ""
    in_product = False
    in_terms = False
    current = ""
    divisor = 1

    with urllib.request.urlopen(url, timeout=900) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace")
            if not in_terms:
                if line.startswith('  "terms"'):
                    in_terms = True
                    continue
                if in_product:
                    block.append(line)
                    if line.startswith("    }"):
                        in_product = False
                        target = classify("".join(block))
                        if target is not None:
                            wanted[sku] = target
                    continue
                m = _PRODUCT_START.match(line)
                if m:
                    sku, block, in_product = m.group(1), [line], True
                continue
            m = _TERM_SKU.match(line)
            if m:
                current = m.group(1) if m.group(1) in wanted else ""
                divisor = 1
                continue
            if current:
                m = _UNIT.match(line)
                if m:
                    divisor = _UNIT_DIVISOR.get(m.group(1), 1)
                    continue
                m = _USD.match(line)
                if m:
                    rate = _trim(m.group(1))
                    if float(rate) == 0:
                        continue  # a free allowance tier, not this sku's rate
                    if divisor != 1:
                        rate = _trim(f"{Decimal(rate) / divisor:f}")
                    bucket, key = wanted.pop(current)
                    if bucket == "flat":
                        into[key] = rate
                    else:
                        into.setdefault(bucket, {})[key] = rate
                    current = ""


def _write(regions: dict[str, dict]) -> None:
    OUT.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": "AWS Price List bulk API - on-demand, USD, Linux/shared tenancy",
                "base_region": "us-east-1",
                "regions": regions,
            },
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )
    print(f"wrote {OUT} ({len(regions)} regions)", file=sys.stderr)


def _s3_only(regions: list[str]) -> None:
    """Refresh s3_gb_month in place, leaving every other rate alone."""
    table = json.loads(OUT.read_text())
    have: dict[str, dict] = table["regions"]
    urls = _region_urls("AmazonS3")
    gda = _region_urls(_S3_GDA_OFFER)
    for region in regions or sorted(have):
        if region not in have or region not in urls:
            print(f"  {region} not in the table, skipped", file=sys.stderr)
            continue
        print(f"{region} ...", file=sys.stderr, flush=True)
        row: dict = {}
        _scan(urls[region], row, _classify_s3)
        if region in gda:
            _scan(gda[region], row, _classify_s3_gda)
        rates = row.get("s3_gb_month")
        if not rates:  # keep the old rates rather than blanking them
            print(f"  no s3 rates for {region}, kept", file=sys.stderr)
            continue
        have[region]["s3_gb_month"] = rates
    _write(have)


def _ddb_only(regions: list[str]) -> None:
    """Refresh the dynamodb rates in place, leaving every other rate alone."""
    table = json.loads(OUT.read_text())
    have: dict[str, dict] = table["regions"]
    urls = _region_urls(_DDB_OFFER)
    for region in regions or sorted(have):
        if region not in have or region not in urls:
            print(f"  {region} not in the table, skipped", file=sys.stderr)
            continue
        print(f"{region} ...", file=sys.stderr, flush=True)
        row: dict = {}
        _scan(urls[region], row, _classify_ddb)
        rates = row.get("dynamodb")
        if len(rates or {}) < len(_DDB_SKUS):  # a partial read is worse than none
            print(f"  incomplete dynamodb rates for {region}, kept", file=sys.stderr)
            continue
        have[region]["dynamodb"] = rates
    _write(have)


def main(argv: list[str]) -> None:
    if "--s3" in argv:
        _s3_only([a for a in argv if a != "--s3"])
        return
    if "--dynamodb" in argv:
        _ddb_only([a for a in argv if a != "--dynamodb"])
        return

    ec2 = _region_urls("AmazonEC2")
    vpc = _region_urls("AmazonVPC")
    elb = _region_urls("AWSELB")
    s3 = _region_urls("AmazonS3")
    gda = _region_urls(_S3_GDA_OFFER)
    ddb = _region_urls(_DDB_OFFER)
    targets = argv or sorted(set(ec2) & set(vpc) & set(elb))

    out: dict[str, dict] = {}
    for region in targets:
        print(f"{region} ...", file=sys.stderr, flush=True)
        row: dict = {}
        for urls in (ec2, vpc, elb):
            _scan(urls[region], row)
        if region in s3:
            _scan(s3[region], row, _classify_s3)
        if region in gda:
            _scan(gda[region], row, _classify_s3_gda)
        if region in ddb:
            _scan(ddb[region], row, _classify_ddb)
        if not row.get("ec2_hourly"):
            print(f"  no ec2 rates for {region}, skipped", file=sys.stderr)
            continue
        out[region] = row

    _write(out)


if __name__ == "__main__":
    main(sys.argv[1:])
