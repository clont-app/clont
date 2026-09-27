"""Tag-hygiene report: cost-bearing resources missing required tags.

Untagged resources are the root cause of unattributable spend — you can't show
back, budget, or find an owner for a cost you can't label. This collector checks
every EC2 instance, EBS volume, RDS instance, load balancer, Lambda function and
S3 bucket for the tag keys the operator declares mandatory
(``finops.required_tags``) and flags any that are missing one.

It is the other half of `finops/showback.py`: showback says *how much* spend has
no owner, this says *which resources* to fix.

Governance, not direct savings, so the estimate is left unset. Config-gated: with
no ``required_tags`` configured the collector is a no-op.

Per service the reads are isolated, because a read-only role often has ec2 but
not lambda — a denied API should cost that one service, not the whole report.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from botocore.exceptions import ClientError

from clont.core.logging import get_logger
from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.base import FinOpsTuning
from clont.finops.models import CostRecord, Recommendation
from clont.providers.aws.parsing import _EBSVolume, _Instance, _LoadBalancer, _RDSInstance
from clont.providers.aws.regions import for_each_region
from clont.providers.base import Provider

log = get_logger("clont.finops.aws.tags")

_KIND = "missing-tags"
_WHAT = "tag hygiene"
# s3 and get_bucket_location are global; any region answers
_GLOBAL_REGION = "us-east-1"
_NO_TAGS = {"NoSuchTagSet", "NoSuchTagSetError"}
# describe_tags takes at most 20 arns per call
_ELB_BATCH = 20


@register("finops", Cloud.AWS, "tags")
class TagHygieneCollector:
    cloud = Cloud.AWS
    service = "tags"

    def __init__(self, provider: Provider, tuning: FinOpsTuning | None = None) -> None:
        self._provider = provider
        self._required = tuple(tuning.required_tags) if tuning else ()

    def collect(self, period: Period) -> list[CostRecord]:
        return []

    def recommendations(self, period: Period) -> list[Recommendation]:
        if not self._required:  # nothing mandated -> nothing to check
            return []
        out = for_each_region(self._provider, self._region, what=_WHAT)
        out.extend(self._safe("s3", self._buckets))  # buckets are global, once
        return out

    def _region(self, region: str) -> list[Recommendation]:
        ec2 = self._provider.client("ec2", region)
        out: list[Recommendation] = []
        out.extend(self._safe("ec2", lambda: self._instances(ec2, region)))
        out.extend(self._safe("ebs", lambda: self._volumes(ec2, region)))
        out.extend(self._safe("rds", lambda: self._databases(region)))
        out.extend(self._safe("elb", lambda: self._load_balancers(region)))
        out.extend(self._safe("lambda", lambda: self._functions(region)))
        return out

    def _safe(
        self, what: str, fn: Callable[[], list[Recommendation]]
    ) -> list[Recommendation]:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - one denied api must not sink the rest
            log.warning("%s: skipping %s for %s: %s", _WHAT, what, self._provider.alias, exc)
            return []

    def _instances(self, ec2, region: str) -> list[Recommendation]:
        out: list[Recommendation] = []
        for page in ec2.get_paginator("describe_instances").paginate():
            for reservation in page.get("Reservations", []):
                for raw in reservation.get("Instances", []):
                    inst = _Instance.model_validate(raw)
                    if inst.state == "terminated":  # going away — don't nag
                        continue
                    missing = self._missing(inst.tag_map())
                    if missing:
                        out.append(self._rec("ec2", inst.instance_id, region, missing))
        return out

    def _volumes(self, ec2, region: str) -> list[Recommendation]:
        out: list[Recommendation] = []
        for page in ec2.get_paginator("describe_volumes").paginate():
            for raw in page.get("Volumes", []):
                vol = _EBSVolume.model_validate(raw)
                missing = self._missing(vol.tag_map())
                if missing:
                    out.append(self._rec("ebs", vol.volume_id, region, missing))
        return out

    def _databases(self, region: str) -> list[Recommendation]:
        rds = self._provider.client("rds", region)
        out: list[Recommendation] = []
        for page in rds.get_paginator("describe_db_instances").paginate():
            for raw in page.get("DBInstances", []):
                db = _RDSInstance.model_validate(raw)
                missing = self._missing(db.tag_map())
                if missing:
                    out.append(self._rec("rds", db.instance_id, region, missing))
        return out

    def _load_balancers(self, region: str) -> list[Recommendation]:
        elb = self._provider.client("elbv2", region)
        names: dict[str, str] = {}
        for page in elb.get_paginator("describe_load_balancers").paginate():
            for raw in page.get("LoadBalancers", []):
                lb = _LoadBalancer.model_validate(raw)
                if lb.arn:
                    names[lb.arn] = lb.name or lb.arn
        out: list[Recommendation] = []
        arns = list(names)
        for batch in (arns[i : i + _ELB_BATCH] for i in range(0, len(arns), _ELB_BATCH)):
            for desc in elb.describe_tags(ResourceArns=batch).get("TagDescriptions", []):
                arn = desc.get("ResourceArn", "")
                missing = self._missing(_pairs(desc.get("Tags", [])))
                if missing:
                    out.append(self._rec("elb", names.get(arn, arn), region, missing))
        return out

    def _functions(self, region: str) -> list[Recommendation]:
        # list_functions doesn't return tags, so it's one extra (free) call per
        # function — the only place this collector fans out per resource
        lam = self._provider.client("lambda", region)
        out: list[Recommendation] = []
        for page in lam.get_paginator("list_functions").paginate():
            for raw in page.get("Functions", []):
                arn = raw.get("FunctionArn", "")
                name = raw.get("FunctionName") or arn
                if not arn:
                    continue
                tags = lam.list_tags(Resource=arn).get("Tags", {}) or {}
                missing = self._missing(tags)
                if missing:
                    out.append(self._rec("lambda", name, region, missing))
        return out

    def _buckets(self) -> list[Recommendation]:
        s3 = self._provider.client("s3", _GLOBAL_REGION)
        out: list[Recommendation] = []
        for raw in s3.list_buckets().get("Buckets", []):
            name = raw.get("Name")
            if name:
                out.extend(self._safe(f"s3 {name}", lambda n=name: self._bucket(s3, n)))
        return out

    def _bucket(self, s3, name: str) -> list[Recommendation]:
        region = _bucket_region(s3, name)
        # tagging is answered by the bucket's own region; a us-east-1 client
        # gets a 301 for anything else
        client = self._provider.client("s3", region)
        try:
            tags = _pairs(client.get_bucket_tagging(Bucket=name).get("TagSet", []))
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) not in _NO_TAGS:
                raise
            tags = {}  # no tag set at all is the most common miss
        missing = self._missing(tags)
        return [self._rec("s3", name, region, missing)] if missing else []

    def _missing(self, tags: dict[str, str]) -> list[str]:
        """Required keys absent or blank on this resource, in declared order."""
        return [k for k in self._required if not tags.get(k)]

    def _rec(self, service: str, rid: str, region: str, missing: list[str]) -> Recommendation:
        return Recommendation(
            cloud=str(Cloud.AWS),
            service=service,
            kind=_KIND,
            resource=CloudResource(
                cloud=Cloud.AWS,
                service=service,
                resource_id=rid,
                region=region,
                alias=self._provider.alias,
            ),
            summary=f"Missing required tag(s): {', '.join(missing)} — add for cost attribution",
            estimated_savings=Money(amount=Decimal(0)),
        )


def _pairs(tags: list[dict]) -> dict[str, str]:
    return {t.get("Key", ""): t.get("Value", "") for t in tags if t.get("Key")}


def _bucket_region(s3, name: str) -> str:
    # legacy api: us-east-1 comes back as null, and "EU" means eu-west-1
    location = s3.get_bucket_location(Bucket=name).get("LocationConstraint")
    if not location:
        return _GLOBAL_REGION
    return "eu-west-1" if location == "EU" else str(location)
