"""FinOps idle detection for load balancers (no registered targets).

An ALB/NLB bills a fixed hourly charge for as long as it exists. One with no
registered targets in any of its target groups can serve nothing — it's pure
waste. This is deterministic (no metrics): list the LB's target groups, and if
none has a single registered target, recommend deleting it. The saving is the
LB's fixed monthly charge.

A load balancer whose targets are all *unhealthy* is a different problem (an
outage, caught by monitoring), so it is deliberately not flagged here — only
genuinely empty ones are.
"""

from __future__ import annotations

from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.aws import pricing
from clont.finops.models import CostRecord, Recommendation
from clont.providers.aws.parsing import _LoadBalancer
from clont.providers.aws.regions import for_each_region
from clont.providers.base import Provider

_KIND = "idle-elb"
_USD = "USD"


@register("finops", Cloud.AWS, "idle_elb")
class IdleLoadBalancerCollector:
    cloud = Cloud.AWS
    service = "idle_elb"

    def __init__(self, provider: Provider, tuning=None) -> None:
        self._provider = provider

    def collect(self, period: Period) -> list[CostRecord]:
        return []

    def recommendations(self, period: Period) -> list[Recommendation]:
        return for_each_region(self._provider, self._region, what="idle elb")

    def _region(self, region: str) -> list[Recommendation]:
        elb = self._provider.client("elbv2", region)
        out: list[Recommendation] = []
        for page in elb.get_paginator("describe_load_balancers").paginate():
            for raw in page.get("LoadBalancers", []):
                lb = _LoadBalancer.model_validate(raw)
                if lb.state and lb.state != "active":
                    continue  # provisioning/failed — let it settle, not "idle"
                if not self._has_targets(elb, lb.arn):
                    out.append(self._rec(lb, region))
        return out

    def _has_targets(self, elb, lb_arn: str) -> bool:
        """True if any of the LB's target groups has a registered target."""
        groups = elb.describe_target_groups(LoadBalancerArn=lb_arn).get("TargetGroups", [])
        for tg in groups:
            health = elb.describe_target_health(
                TargetGroupArn=tg["TargetGroupArn"]
            ).get("TargetHealthDescriptions", [])
            if health:
                return True
        return False

    def _rec(self, lb: _LoadBalancer, region: str) -> Recommendation:
        kind = (lb.lb_type or "load balancer").upper()
        return Recommendation(
            cloud=str(Cloud.AWS),
            service="elb",
            kind=_KIND,
            resource=CloudResource(
                cloud=Cloud.AWS,
                service="elb",
                resource_id=lb.name or lb.arn,
                region=region,
                alias=self._provider.alias,
            ),
            summary=f"{kind} load balancer has no registered targets — delete if unused",
            estimated_savings=Money(amount=pricing.LOAD_BALANCER_MONTH, currency=_USD),
        )
