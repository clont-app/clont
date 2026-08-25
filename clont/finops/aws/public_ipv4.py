"""FinOps for public IPv4 addresses — AWS bills every one of them.

Since Feb 2024 a public IPv4 costs $0.005/hr whether it's doing anything or
not, which makes this a headcount problem: every billable address in a VPC
hangs off an ENI, so one paginated ``describe_network_interfaces`` per region
sees NAT gateways, load balancers, instances, RDS and Fargate at once.
``describe_addresses`` adds the unassociated EIPs, which have no ENI.

Two free describes per region — no Cost Explorer, no CloudWatch, no runtime
Price List call.

`collect()` reports what every address is costing; `recommendations()` flags
only the deterministic waste. A public IP on a production load balancer is a
cost, not a mistake, and conflating the two is how the tool loses trust.
"""

from __future__ import annotations

from datetime import date

from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.aws import pricing
from clont.finops.models import CostRecord, Recommendation
from clont.providers.aws.parsing import _EIP, _NetworkInterface
from clont.providers.aws.regions import for_each_region
from clont.providers.base import Provider

_USD = "USD"
_SERVICE = "public_ipv4"
# distinct from the CUR's own `ec2` line, which is real billed spend — sharing
# a key would double-count this estimate into the top-spend table
_USAGE_TYPE = "PublicIPv4:InUseAddress"
_WHAT = "public ipv4"


@register("finops", Cloud.AWS, "public_ipv4")
class PublicIPv4Collector:
    cloud = Cloud.AWS
    service = _SERVICE
    collect_every_seconds = 3600
    recommend_every_seconds = 86400

    def __init__(self, provider: Provider, tuning=None) -> None:
        self._provider = provider

    def collect(self, period: Period) -> list[CostRecord]:
        # the describes see *now*, so this is the last day's run-rate, stamped
        # per-day like cur/ce — the digest and spike detectors key on period.end
        day = period.end
        return for_each_region(
            self._provider, lambda r: self._collect_region(r, day), what=_WHAT
        )

    def recommendations(self, period: Period) -> list[Recommendation]:
        return for_each_region(self._provider, self._recommend_region, what=_WHAT)

    def _collect_region(self, region: str, day: date) -> list[CostRecord]:
        ec2 = self._provider.client("ec2", region)
        addresses = self._billable(ec2)
        if not addresses:
            return []
        quote = pricing.public_ipv4_daily_quote(region)
        return [
            CostRecord(
                cloud=str(Cloud.AWS),
                service=_SERVICE,
                period=Period(start=day, end=day),
                alias=self._provider.alias,
                cost=Money(amount=quote.amount * len(addresses), currency=_USD),
                dimensions={
                    "usage_type": _USAGE_TYPE,
                    "region": region,
                    "addresses": str(len(addresses)),
                },
            )
        ]

    def _billable(self, ec2) -> set[str]:
        """Every charged address in the region, deduped by IP.

        An associated EIP shows up in *both* describes; counting it twice would
        double the headline figure. A byoip address is on its eni too, so it has
        to be subtracted at the end rather than just skipped in the eip loop.
        """
        ips: set[str] = set()
        byoip: set[str] = set()
        for eip in self._eips(ec2):
            if eip.public_ip:
                (byoip if eip.byoip else ips).add(eip.public_ip)
        for eni in self._interfaces(ec2):
            ips.update(eni.public_ips())
        return ips - byoip

    def _recommend_region(self, region: str) -> list[Recommendation]:
        ec2 = self._provider.client("ec2", region)
        quote = pricing.public_ipv4_quote(region)
        eips = list(self._eips(ec2))
        # not billed, so never worth a "release it to save $x"
        byoip = {e.public_ip for e in eips if e.byoip and e.public_ip}
        out: list[Recommendation] = []

        for eni in self._interfaces(ec2):
            ips = [ip for ip in eni.public_ips() if ip not in byoip]
            if ips and eni.unattached():
                out.append(self._rec(
                    "unattached-eni-ip", eni.eni_id, region, quote,
                    f"Public IPv4 {ips[0]} on {eni.eni_id}, "
                    f"status {eni.status or eni.attachment_status} — release it",
                ))
            for extra in eni.secondary_public_ips():
                if extra in byoip:
                    continue
                out.append(self._rec(
                    # the ip is part of the id: events dedupe on kind+resource_id,
                    # so two secondaries on one eni would collapse into one alert
                    "secondary-public-ip", f"{eni.eni_id}/{extra}", region, quote,
                    f"Secondary public IPv4 {extra} on {eni.eni_id} "
                    "— billed on top of the primary",
                ))

        for eip in eips:
            if eip.association_id or eip.byoip:
                continue  # in use, or not ours to be billed for
            out.append(self._rec(
                "unassociated-eip", eip.allocation_id or eip.public_ip, region, quote,
                f"Unassociated Elastic IP {eip.public_ip} — release",
            ))
        return out

    def _interfaces(self, ec2):
        for page in ec2.get_paginator("describe_network_interfaces").paginate():
            for raw in page.get("NetworkInterfaces", []):
                yield _NetworkInterface.model_validate(raw)

    def _eips(self, ec2):
        for raw in ec2.describe_addresses().get("Addresses", []):
            yield _EIP.model_validate(raw)

    def _rec(self, kind: str, rid: str, region: str, quote, summary: str) -> Recommendation:
        return Recommendation(
            cloud=str(Cloud.AWS),
            service="ec2",
            kind=kind,
            resource=CloudResource(
                cloud=Cloud.AWS,
                service="ec2",
                resource_id=rid,
                region=region,
                alias=self._provider.alias,
            ),
            summary=summary,
            estimated_savings=Money(amount=quote.amount, currency=_USD),
            priced_region=quote.region,
            approximate=quote.approximate,
        )
