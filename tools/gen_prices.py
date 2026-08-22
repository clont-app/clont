#!/usr/bin/env python3
"""Regenerate clont/finops/aws/prices.json from the AWS Price List bulk API.

Offline, run by hand at release time — it is not shipped in the wheel and clont
never calls it at runtime. The bulk API is free, needs no credentials, no IAM.

    python tools/gen_prices.py                 # every region
    python tools/gen_prices.py us-east-1 eu-west-1

The EC2 region shards are ~480 MB of pretty-printed JSON each, so they are
scanned line by line and never held in memory — `json.load` on one wants more
RAM than most machines will give it. Products come before terms in the file, so
a single sequential pass collects the SKUs it wants, then their rates.

Everything but the load balancer lives in the AmazonEC2 offer (NAT gateway and
EBS included); only the ALB hourly comes from AWSELB. AmazonVPC carries the idle
public IPv4 charge.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

BULK = "https://pricing.us-east-1.amazonaws.com"
OUT = Path(__file__).resolve().parent.parent / "clont" / "finops" / "aws" / "prices.json"

# one rate per family at .large; other sizes scale by the normalization factor
_LARGE = re.compile(r"^([a-z0-9\-]+)\.large$")
_PRODUCT_START = re.compile(r'^ {4}"([A-Z0-9]{10,20})" : \{')
_TERM_SKU = re.compile(r'^ {6}"([A-Z0-9]{10,20})" : \{')
_USD = re.compile(r'^\s*"USD" : "([0-9.]+)"')

_EBS_TYPES = {"gp3", "gp2", "io1", "io2", "st1", "sc1", "standard"}


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
    if family == "Compute Instance":
        if (
            _attr(block, "operatingSystem") != "Linux"
            or _attr(block, "tenancy") != "Shared"
            or _attr(block, "preInstalledSw") != "NA"
            or _attr(block, "capacitystatus") != "Used"
            or _attr(block, "licenseModel") != "No License required"
        ):
            return None
        m = _LARGE.match(_attr(block, "instanceType"))
        return ("ec2_family_large_hourly", m.group(1)) if m else None
    if family == "Storage":
        vol = _attr(block, "volumeApiName")
        return ("ebs_gb_month", vol) if vol in _EBS_TYPES else None
    if family == "Storage Snapshot" and usagetype.endswith("EBS:SnapshotUsage"):
        return ("flat", "snapshot_gb_month")
    if family == "NAT Gateway" and usagetype.endswith("NatGateway-Hours"):
        return ("flat", "nat_gateway_hourly")
    if usagetype.endswith("PublicIPv4:IdleAddress"):
        return ("flat", "eip_hourly")
    # the plain ALB hourly - not Outposts-, not TS- (Local Zones)
    if family == "Load Balancer-Application" and usagetype.endswith("LoadBalancerUsage"):
        if "Outposts-" in usagetype or "TS-" in usagetype:
            return None
        return ("flat", "load_balancer_hourly")
    return None


def _trim(rate: str) -> str:
    """0.0960000000 -> 0.096. the table is read by humans in review."""
    return rate.rstrip("0").rstrip(".") if "." in rate else rate


def _scan(url: str, into: dict) -> None:
    """Stream one region shard, folding the rates we recognise into `into`."""
    wanted: dict[str, tuple[str, str]] = {}
    block: list[str] = []
    sku = ""
    in_product = False
    in_terms = False
    current = ""

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
                        target = _classify("".join(block))
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
                continue
            if current:
                m = _USD.match(line)
                if m:
                    bucket, key = wanted.pop(current)
                    rate = _trim(m.group(1))
                    if bucket == "flat":
                        into[key] = rate
                    else:
                        into.setdefault(bucket, {})[key] = rate
                    current = ""


def main(regions: list[str]) -> None:
    ec2 = _region_urls("AmazonEC2")
    vpc = _region_urls("AmazonVPC")
    elb = _region_urls("AWSELB")
    targets = regions or sorted(set(ec2) & set(vpc) & set(elb))

    out: dict[str, dict] = {}
    for region in targets:
        print(f"{region} ...", file=sys.stderr, flush=True)
        row: dict = {}
        for urls in (ec2, vpc, elb):
            _scan(urls[region], row)
        if not row.get("ec2_family_large_hourly"):
            print(f"  no ec2 rates for {region}, skipped", file=sys.stderr)
            continue
        out[region] = row

    OUT.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": "AWS Price List bulk API - on-demand, USD, Linux/shared tenancy",
                "base_region": "us-east-1",
                "regions": out,
            },
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )
    print(f"wrote {OUT} ({len(out)} regions)", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:])
