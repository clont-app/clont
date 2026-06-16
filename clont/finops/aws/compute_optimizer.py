"""FinOps recommendations from AWS Compute Optimizer (rightsizing).

Compute Optimizer does the analysis; we just read its recommendations per region
for EC2 instances, EBS volumes, Auto Scaling groups, Lambda functions, ECS
services and RDS databases, surfacing the ones that save money (monthly savings
> 0) as `Recommendation`s. The account must be opted into Compute Optimizer; if
it isn't, the calls are denied and we log that once per cycle rather than warning
per region.

Regional service, so it iterates the provider's regions like the monitoring
collectors. `collect()` is a no-op — this collector only produces
recommendations, not cost records.
"""

from __future__ import annotations

from collections.abc import Callable

from botocore.exceptions import ClientError

from clont.core.logging import get_logger
from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.models import CostRecord, Recommendation
from clont.providers.aws.parsing import (
    _COASGRec,
    _COECSRec,
    _COInstanceRec,
    _COLambdaRec,
    _CORDSRec,
    _COOption,
    _COVolumeRec,
)
from clont.providers.aws.regions import for_each_region
from clont.providers.base import Provider

log = get_logger("clont.finops.aws.compute_optimizer")

_KIND = "rightsize"
# Errors that just mean "Compute Optimizer isn't enabled for this account" —
# expected, not a failure, so logged once per cycle at info (the collector is
# rebuilt each cycle, resetting the flag) instead of a per-region warning.
_NOT_AVAILABLE = {"OptInRequiredException", "AccessDeniedException", "AccessDenied"}


def _best_savings_option(options: list[_COOption]) -> _COOption | None:
    """The recommended option that actually saves money (lowest rank wins)."""
    saving = [o for o in options if o.savings > 0]
    if not saving:
        return None
    return min(saving, key=lambda o: o.rank)  # unranked defaults to 999 (sorts last)


def _resource_id(arn: str) -> str:
    """The resource's own id from its ARN.

    Most ARNs put the name after the last ``/`` (EC2, EBS, ASG, ECS); Lambda and
    RDS use ``:`` instead. Fall back to the whole ARN if neither is present.
    """
    if not arn:
        return arn
    if "/" in arn:
        return arn.rsplit("/", 1)[-1]
    return arn.rsplit(":", 1)[-1]


def _rightsize_summary(rec, best: _COOption) -> str:
    """Generic summary for resources whose target is just a % saving."""
    return f"{rec.finding or 'Rightsizable'}: rightsize (~{best.pct:.0f}% saving)"


@register("finops", Cloud.AWS, "compute_optimizer")
class ComputeOptimizerCollector:
    cloud = Cloud.AWS
    service = "compute_optimizer"

    # (service, method, list_key, parse model, summary fn) per resource type.
    # Each type is opted into independently in Compute Optimizer, so each is
    # collected (and degrades) on its own — see `_collect_recs`.
    _SOURCES = (
        ("ec2", "get_ec2_instance_recommendations", "instanceRecommendations",
         _COInstanceRec,
         lambda r, b: f"{r.finding}: {r.current_type or '?'} -> "
                      f"{b.instance_type or '?'} (~{b.pct:.0f}% saving)"),
        ("ebs", "get_ebs_volume_recommendations", "volumeRecommendations",
         _COVolumeRec,
         lambda r, b: f"{r.finding}: resize volume (~{b.pct:.0f}% saving)"),
        ("asg", "get_auto_scaling_group_recommendations",
         "autoScalingGroupRecommendations", _COASGRec, _rightsize_summary),
        ("lambda", "get_lambda_function_recommendations",
         "lambdaFunctionRecommendations", _COLambdaRec, _rightsize_summary),
        ("ecs", "get_ecs_service_recommendations",
         "ecsServiceRecommendations", _COECSRec, _rightsize_summary),
        ("rds", "get_rds_database_recommendations",
         "rdsDBRecommendations", _CORDSRec, _rightsize_summary),
    )

    def __init__(self, provider: Provider, tuning=None) -> None:
        self._provider = provider
        self._warned_unavailable: set[str] = set()  # services already logged as off

    def collect(self, period: Period) -> list[CostRecord]:
        return []  # rightsizing advice only, no spend records

    def recommendations(self, period: Period) -> list[Recommendation]:
        return for_each_region(
            self._provider, self._region_recommendations, what="compute optimizer"
        )

    def _region_recommendations(self, region: str) -> list[Recommendation]:
        co = self._provider.client("compute-optimizer", region)
        out: list[Recommendation] = []
        for service, method, list_key, model, summarize in self._SOURCES:
            # Isolate each resource type: a transient error on one (e.g. a
            # throttle on Lambda) must not discard the recs already gathered for
            # the others in this region. Expected "not enabled" errors are
            # already handled inside `_collect_recs`.
            try:
                out.extend(
                    self._collect_recs(co, service, method, list_key, model, region, summarize)
                )
            except Exception as exc:  # noqa: BLE001 - one source must not drop the rest
                log.warning(
                    "compute optimizer %s failed in %s for %s: %s",
                    service, region, self._provider.alias, exc,
                )
        return out

    def _collect_recs(
        self,
        co,
        service: str,
        method: str,
        list_key: str,
        model,
        region: str,
        summarize: Callable,
    ) -> list[Recommendation]:
        """Read one resource type's recommendations.

        A type the account hasn't opted into (or lacks permission for) is logged
        once and skipped without affecting the others; any other error propagates
        to `for_each_region`, which isolates the region.
        """
        out: list[Recommendation] = []
        fn = getattr(co, method)
        token: str | None = None
        while True:
            try:
                resp = fn(**({"nextToken": token} if token else {}))
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in _NOT_AVAILABLE:
                    if service not in self._warned_unavailable:
                        log.info(
                            "compute optimizer (%s) not enabled for %s: %s",
                            service, self._provider.alias, exc,
                        )
                        self._warned_unavailable.add(service)
                    return []
                raise  # real error -> let for_each_region log + skip this region
            for raw in resp.get(list_key, []):
                rec = model.model_validate(raw)
                best = _best_savings_option(rec.options)
                if best is None:
                    continue
                out.append(self._rec(service, rec.arn, region, summarize(rec, best), best))
            token = resp.get("nextToken")
            if not token:
                break
        return out

    def _rec(
        self, service: str, arn: str, region: str, summary: str, option: _COOption
    ) -> Recommendation:
        return Recommendation(
            cloud=str(Cloud.AWS),
            service=service,
            kind=_KIND,
            resource=CloudResource(
                cloud=Cloud.AWS,
                service=service,
                resource_id=_resource_id(arn),
                region=region,
                alias=self._provider.alias,
            ),
            summary=summary,
            # Gross (on-demand-equivalent) saving; savingsOpportunityAfterDiscounts
            # would net out existing RIs/SPs but we report the headline figure.
            estimated_savings=Money(amount=option.savings, currency=option.currency),
        )
