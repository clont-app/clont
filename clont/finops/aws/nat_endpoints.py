"""NAT gateway paying for traffic a free gateway endpoint would carry.

The classic transfer finding: s3 and dynamodb traffic from a private subnet goes
out through the NAT gateway, which bills per gigabyte processed *on top of* the
hourly charge. An s3/dynamodb **gateway** endpoint carries the same traffic for
nothing — no hourly charge, no processing charge. A vpc that has a NAT gateway
and no gateway endpoint is therefore paying for bytes it doesn't have to.

Deterministic and free: `describe_nat_gateways` + `describe_vpc_endpoints`, no
metrics and no cur. The saving depends on how much of that traffic is actually
s3/dynamodb, which neither call can say, so no dollar figure is invented — the
transfer report's `nat` bucket is the number to read next to this.
"""

from __future__ import annotations

from decimal import Decimal

from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.models import CostRecord, Recommendation
from clont.providers.aws.parsing import _NatGateway
from clont.providers.aws.regions import for_each_region
from clont.providers.base import Provider

_KIND = "nat-gateway-endpoint"
_USD = "USD"
# gateway endpoints exist for exactly these two services; everything else is
# interface (privatelink), which bills its own hourly charge and is not free
_GATEWAY_SERVICES = ("s3", "dynamodb")


@register("finops", Cloud.AWS, "nat_endpoints")
class NatGatewayEndpointCollector:
    cloud = Cloud.AWS
    service = "nat_endpoints"
    recommend_every_seconds = 86400

    def __init__(self, provider: Provider, tuning=None) -> None:
        self._provider = provider

    def collect(self, period: Period) -> list[CostRecord]:
        return []

    def recommendations(self, period: Period) -> list[Recommendation]:
        return for_each_region(self._provider, self._region, what="nat endpoints")

    def _region(self, region: str) -> list[Recommendation]:
        ec2 = self._provider.client("ec2", region)
        vpcs = self._nat_vpcs(ec2)
        if not vpcs:
            return []
        endpoints = self._gateway_endpoints(ec2)
        out: list[Recommendation] = []
        for vpc_id, nat_ids in sorted(vpcs.items()):
            missing = [s for s in _GATEWAY_SERVICES if s not in endpoints.get(vpc_id, set())]
            if missing:
                out.append(self._rec(vpc_id, nat_ids, missing, region))
        return out

    def _nat_vpcs(self, ec2) -> dict[str, list[str]]:
        """vpc id -> its available NAT gateways."""
        vpcs: dict[str, list[str]] = {}
        for page in ec2.get_paginator("describe_nat_gateways").paginate():
            for raw in page.get("NatGateways", []):
                nat = _NatGateway.model_validate(raw)
                if nat.state != "available" or not nat.vpc_id:
                    continue
                vpcs.setdefault(nat.vpc_id, []).append(nat.nat_gateway_id)
        return vpcs

    def _gateway_endpoints(self, ec2) -> dict[str, set[str]]:
        """vpc id -> the gateway-endpoint services it already has."""
        found: dict[str, set[str]] = {}
        pages = ec2.get_paginator("describe_vpc_endpoints").paginate(
            Filters=[{"Name": "vpc-endpoint-type", "Values": ["Gateway"]}]
        )
        for page in pages:
            for raw in page.get("VpcEndpoints", []):
                vpc_id = raw.get("VpcId") or ""
                # com.amazonaws.<region>.s3 -> s3
                name = str(raw.get("ServiceName") or "").rsplit(".", 1)[-1].lower()
                if vpc_id and name:
                    found.setdefault(vpc_id, set()).add(name)
        return found

    def _rec(
        self, vpc_id: str, nat_ids: list[str], missing: list[str], region: str
    ) -> Recommendation:
        return Recommendation(
            cloud=str(Cloud.AWS),
            service="vpc",
            kind=_KIND,
            resource=CloudResource(
                cloud=Cloud.AWS,
                service="vpc",
                resource_id=vpc_id,
                region=region,
                alias=self._provider.alias,
            ),
            summary=(
                f"{', '.join(missing)} traffic from {vpc_id} pays NAT processing "
                f"({', '.join(sorted(nat_ids))}) — a gateway endpoint carries it free"
            ),
            # no figure: only cur knows how much of the nat bytes were s3/dynamodb
            estimated_savings=Money(amount=Decimal(0), currency=_USD),
        )
