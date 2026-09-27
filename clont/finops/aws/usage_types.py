"""Data transfer buckets read off `lineItem/UsageType`.

Transfer is normally 5-15% of an aws bill and there is no api for it — the usage
type string is the only free place that says *which* transfer a dollar bought.
Classifying it here keeps cur dumb: it tags each row with a bucket and the report
groups by that.

Buckets are the ones with different fixes: cross-az chatter is a placement
problem, nat processing is usually a missing gateway endpoint, inter-region is
replication, cdn vs direct egress is a routing choice.
"""

from __future__ import annotations

from clont.finops.transfer import DIMENSION  # noqa: F401 - re-exported for cur

NAT = "nat"
CROSS_AZ = "cross-az"
INTER_REGION = "inter-region"
INTERNET_OUT = "internet-out"
INTERNET_IN = "internet-in"
CDN = "cdn"
TRANSIT_GATEWAY = "transit-gateway"
PRIVATELINK = "privatelink"


def transfer_bucket(usage_type: str, service: str = "") -> str:
    """Bucket for one cur row, "" when the row isn't data transfer.

    `service` only settles cloudfront: its egress usage types are spelled exactly
    like plain internet egress (`US-DataTransfer-Out-Bytes`), so the product name
    is the only way to tell a cdn dollar from a direct one.
    """
    u = usage_type.strip()
    # hours, requests, ip-addresses: every transfer meter is in bytes
    if not u or "Bytes" not in u:
        return ""
    if "NatGateway" in u:
        return NAT
    if "TransitGateway" in u:
        return TRANSIT_GATEWAY
    if "VpcEndpoint" in u or "PrivateLink" in u:
        return PRIVATELINK
    if "CloudFront" in service or "CloudFront" in u:
        return CDN
    # region pair, e.g. USE1-USW2-AWS-Out-Bytes
    if "-AWS-In-Bytes" in u or "-AWS-Out-Bytes" in u:
        return INTER_REGION
    if "DataTransfer-Regional-Bytes" in u:
        return CROSS_AZ
    if "DataTransfer-Out" in u:
        return INTERNET_OUT
    if "DataTransfer-In" in u:
        return INTERNET_IN
    return ""
