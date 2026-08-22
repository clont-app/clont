"""FinOps idle detection for NAT gateways (traffic-based).

A NAT gateway bills a fixed hourly charge whether or not anything routes through
it. One that has processed effectively no bytes over a trailing window is paying
for nothing — recommend deleting it (and any route that depends on it). The
saving is the gateway's fixed monthly charge, which is known, so it's reported.

Off unless `finops.allow_cloudwatch_metrics` is set — Compute Optimizer reports
idle NAT gateways for free. Same trade-off as `idle.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.aws import pricing
from clont.finops.base import FinOpsTuning
from clont.finops.models import CostRecord, Recommendation
from clont.providers.aws.metrics import metric_query, metric_values, run_metric_queries
from clont.providers.aws.parsing import _NatGateway
from clont.providers.aws.regions import for_each_region
from clont.providers.base import Provider

_PERIOD = 86400
# Total bytes (in + out) over the window below which a gateway counts as idle.
# Deliberately small but non-zero to tolerate stray health-check traffic.
_IDLE_BYTES = 1_000_000
_METRICS = ("BytesOutToDestination", "BytesInFromSource")
_KIND = "idle-nat"
_USD = "USD"


@register("finops", Cloud.AWS, "idle_nat")
class IdleNatGatewayCollector:
    cloud = Cloud.AWS
    service = "idle_nat"
    recommend_every_seconds = 86400

    def __init__(self, provider: Provider, tuning: FinOpsTuning | None = None) -> None:
        self._provider = provider
        self._tuning = tuning or FinOpsTuning()

    def collect(self, period: Period) -> list[CostRecord]:
        return []

    def recommendations(self, period: Period) -> list[Recommendation]:
        if not self._tuning.allow_cloudwatch_metrics:
            return []  # compute optimizer covers this for free
        return for_each_region(self._provider, self._region, what="idle nat")

    def _region(self, region: str) -> list[Recommendation]:
        ec2 = self._provider.client("ec2", region)
        nat_ids = self._available_nat_ids(ec2)
        if not nat_ids:
            return []
        totals = self._byte_totals(region, nat_ids)
        return [
            self._rec(nat_id, region)
            for nat_id in nat_ids
            if totals.get(nat_id, 0.0) < _IDLE_BYTES
        ]

    def _available_nat_ids(self, ec2) -> list[str]:
        ids: list[str] = []
        for page in ec2.get_paginator("describe_nat_gateways").paginate():
            for raw in page.get("NatGateways", []):
                nat = _NatGateway.model_validate(raw)
                if nat.state == "available":
                    ids.append(nat.nat_gateway_id)
        return ids

    def _byte_totals(self, region: str, nat_ids: list[str]) -> dict[str, float]:
        """Total bytes (in + out) per gateway across the window -> {id: bytes}."""
        end = datetime.now(UTC)
        start = end - timedelta(days=self._tuning.idle_lookback_days)

        queries: list[dict] = []
        id_map: dict[str, str] = {}
        for n, (nat_id, metric) in enumerate(
            (nat_id, m) for nat_id in nat_ids for m in _METRICS
        ):
            qid = f"q{n}"
            id_map[qid] = nat_id
            queries.append(
                metric_query(
                    qid, "AWS/NATGateway", metric, "NatGatewayId", nat_id, _PERIOD,
                    stat="Sum",
                )
            )

        cw = self._provider.client("cloudwatch", region)
        series = run_metric_queries(cw, queries, start, end)
        totals: dict[str, float] = {nat_id: 0.0 for nat_id in nat_ids}
        for qid, nat_id in id_map.items():
            totals[nat_id] += sum(metric_values(series, qid))
        return totals

    def _rec(self, nat_id: str, region: str) -> Recommendation:
        return Recommendation(
            cloud=str(Cloud.AWS),
            service="ec2",
            kind=_KIND,
            resource=CloudResource(
                cloud=Cloud.AWS,
                service="ec2",
                resource_id=nat_id,
                region=region,
                alias=self._provider.alias,
            ),
            summary=(
                f"NAT gateway idle {self._tuning.idle_lookback_days}d "
                "(≈0 bytes processed) — delete if unused"
            ),
            estimated_savings=Money(amount=pricing.NAT_GATEWAY_MONTH, currency=_USD),
        )
