"""S3 storage waste: missing lifecycle rules, old versions, abandoned uploads.

Four findings, all from free bucket-level reads (`get_bucket_lifecycle_configuration`,
`get_bucket_versioning`, `list_multipart_uploads`):

* **incomplete multipart uploads** — parts of an upload that never finished are
  billed as storage forever and are invisible in the console. The one finding
  here with a real dollar figure: `list_parts` gives the bytes.
* **noncurrent versions** — a versioned bucket with no expiration rule keeps
  every overwrite for good.
* **standard with no transition** — data sitting in Standard that nothing moves
  to a cheaper class.
* **no lifecycle at all** — the catch-all, emitted only when none of the above
  fired *and* the lifecycle config was readable, so one bucket never produces
  four rows saying the same thing and a denied read is never read as "no rules".

Each check is isolated on its own: `ListMultipartUploadParts`, `GetBucketVersioning`
and CloudWatch are separate grants, so a role that lacks one still gets the rest.

Two things this deliberately does not claim:

* **A prefix- or tag-scoped rule is not coverage.** A rule that aborts uploads
  under ``logs/`` does nothing for the rest of the bucket, so only an unscoped
  rule counts as protecting it.
* **Access frequency is unknowable for free.** S3 request metrics are opt-in and
  billed, so the cold-data finding is a *candidate*: it reports the bytes and
  what a transition would save if the data is cold, and says so.

The sizes come from CloudWatch daily storage metrics, which S3 publishes for
free — but `GetMetricData` bills per metric requested, so the cold-data check is
behind `finops.allow_cloudwatch_metrics` like the idle detectors. Everything else
works without it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from botocore.exceptions import ClientError

from clont.core.logging import get_logger
from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.aws import pricing
from clont.finops.base import FinOpsTuning
from clont.finops.models import CostRecord, Recommendation
from clont.providers.aws.metrics import metric_query_dims, run_metric_queries
from clont.providers.base import Provider

log = get_logger("clont.finops.aws.s3")

_WHAT = "s3 storage"
# s3 and get_bucket_location are global; any region answers
_GLOBAL_REGION = "us-east-1"
_NO_LIFECYCLE = "NoSuchLifecycleConfiguration"
_USD = "USD"
_GIB = Decimal(1024**3)
_PERIOD = 86400  # daily storage metrics; anything finer has no datapoints
# storage metrics land once a day, and late; look back far enough to catch one
_METRIC_DAYS = 3
# list_parts costs a call per upload, so only the oldest few are sized
_MAX_SIZED_UPLOADS = 25


@register("finops", Cloud.AWS, "s3")
class S3StorageCollector:
    cloud = Cloud.AWS
    service = "s3"
    # lifecycle and versioning don't change inside a day
    recommend_every_seconds = 86400

    def __init__(self, provider: Provider, tuning: FinOpsTuning | None = None) -> None:
        self._provider = provider
        self._tuning = tuning or FinOpsTuning()
        self._clients: dict[str, object] = {}

    def collect(self, period: Period) -> list[CostRecord]:
        return []

    def recommendations(self, period: Period) -> list[Recommendation]:
        out: list[Recommendation] = []
        for name in self._safe("list buckets", self._bucket_names, []):
            out.extend(self._safe(f"bucket {name}", lambda n=name: self._bucket(n), []))
        return out

    def _bucket_names(self) -> list[str]:
        s3 = self._client(_GLOBAL_REGION)
        return [b["Name"] for b in s3.list_buckets().get("Buckets", []) if b.get("Name")]

    def _client(self, region: str):
        # a bucket's own region answers its config; a us-east-1 client gets a 301
        if region not in self._clients:
            self._clients[region] = self._provider.client("s3", region)
        return self._clients[region]

    def _safe(self, what: str, fn: Callable[[], object], default=None):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - one denied call must not sink the rest
            log.warning("%s: skipping %s for %s: %s", _WHAT, what, self._provider.alias, exc)
            return default

    def _bucket(self, name: str) -> list[Recommendation]:
        region = self._bucket_region(name)
        s3 = self._client(region)
        # each check below needs its own grant (ListMultipartUploadParts and
        # GetBucketVersioning are separate), so they get separate isolation —
        # one AccessDenied must not drop the findings that did work
        rules = self._safe(f"lifecycle of {name}", lambda: self._rules(s3, name))
        out: list[Recommendation] = []

        checks = (
            ("uploads", lambda: self._multipart(s3, name, region, rules)),
            ("versioning", lambda: self._versions(s3, name, region, rules)),
            ("size", lambda: self._cold(name, region, rules)),
        )
        for what, check in checks:
            out += self._safe(f"{what} of {name}", check, [])
        # only claim "unmanaged" when the rules were actually readable and empty
        if rules == [] and not out:
            out.append(self._no_lifecycle_rec(name, region))
        return out

    def _multipart(self, s3, name: str, region: str, rules: list[dict] | None) -> list:
        uploads = self._old_uploads(s3, name)
        if not uploads:
            return []
        rec = self._multipart_rec(s3, name, region, uploads, rules)
        return [rec] if rec is not None else []

    def _versions(self, s3, name: str, region: str, rules: list[dict] | None) -> list:
        if rules is None or _has_action(rules, "NoncurrentVersionExpiration"):
            return []  # unreadable rules: can't claim the rule is missing
        versioning = self._versioning(s3, name)
        return [self._versions_rec(name, region, versioning)] if versioning else []

    def _cold(self, name: str, region: str, rules: list[dict] | None) -> list:
        rec = self._cold_rec(name, region, rules)
        return [rec] if rec is not None else []

    def _bucket_region(self, name: str) -> str:
        s3 = self._client(_GLOBAL_REGION)
        # legacy api: us-east-1 comes back as null, and "EU" means eu-west-1
        location = s3.get_bucket_location(Bucket=name).get("LocationConstraint")
        if not location:
            return _GLOBAL_REGION
        return "eu-west-1" if location == "EU" else str(location)

    def _rules(self, s3, name: str) -> list[dict]:
        """Enabled lifecycle rules that cover the whole bucket."""
        try:
            raw = s3.get_bucket_lifecycle_configuration(Bucket=name).get("Rules", [])
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) != _NO_LIFECYCLE:
                raise
            return []
        return [r for r in raw if r.get("Status") == "Enabled" and _covers_bucket(r)]

    def _versioning(self, s3, name: str) -> str:
        """"Enabled" / "Suspended" / "" — suspended still keeps the old versions."""
        status = s3.get_bucket_versioning(Bucket=name).get("Status") or ""
        return status if status in ("Enabled", "Suspended") else ""

    def _old_uploads(self, s3, name: str) -> list[dict]:
        """Incomplete uploads past the age threshold, oldest first."""
        cutoff = datetime.now(UTC) - timedelta(
            days=self._tuning.s3_multipart_min_age_days
        )
        out: list[dict] = []
        for page in s3.get_paginator("list_multipart_uploads").paginate(Bucket=name):
            for raw in page.get("Uploads", []):
                started = _aware(raw.get("Initiated"))
                if started is not None and started < cutoff:
                    out.append({**raw, "Initiated": started})
        out.sort(key=lambda u: u["Initiated"])
        return out

    def _upload_bytes(self, s3, name: str, uploads: list[dict]) -> tuple[Decimal, int]:
        """Summed part bytes and how many uploads that covers."""
        total = Decimal(0)
        sized = 0
        for upload in uploads[:_MAX_SIZED_UPLOADS]:
            pages = s3.get_paginator("list_parts").paginate(
                Bucket=name, Key=upload["Key"], UploadId=upload["UploadId"]
            )
            for page in pages:
                for part in page.get("Parts", []):
                    total += Decimal(int(part.get("Size") or 0))
            sized += 1
        return total, sized

    def _cold_bytes(self, name: str, region: str) -> Decimal | None:
        """Latest StandardStorage bytes from CloudWatch, or None if unreadable."""
        # one call per bucket: billing is per metric, so batching per region
        # would only save round-trips
        cw = self._provider.client("cloudwatch", region)
        end = datetime.now(UTC)
        query = metric_query_dims(
            "std",
            "AWS/S3",
            "BucketSizeBytes",
            {"BucketName": name, "StorageType": "StandardStorage"},
            _PERIOD,
        )
        series = run_metric_queries(
            cw, [query], end - timedelta(days=_METRIC_DAYS), end
        )
        points = series.get("std") or []
        if not points:
            return None
        return Decimal(str(max(points, key=lambda p: p[0])[1]))

    def _multipart_rec(
        self, s3, name: str, region: str, uploads: list[dict], rules: list[dict] | None
    ) -> Recommendation | None:
        total, sized = self._upload_bytes(s3, name, uploads)
        if total == 0 and sized == len(uploads):
            return None  # initiated, never uploaded a part: nothing is billed
        gib = total / _GIB
        oldest = (datetime.now(UTC) - uploads[0]["Initiated"]).days
        # parts keep the class of the upload that made them
        api_class = str(uploads[0].get("StorageClass") or "STANDARD")
        storage_class = pricing.S3_API_CLASS.get(api_class)
        if storage_class is None:
            # standard is the priciest class, so guessing it inflates the saving
            quote = None
            note = f", class {api_class} not priced"
        else:
            quote = pricing.s3_storage_quote(gib, storage_class, region)
            note = ", at us-east-1 rates" if quote.approximate else ""
        scope = "" if sized == len(uploads) else f" (sized the oldest {sized})"
        size = _human(total)
        fix = (
            " — add an AbortIncompleteMultipartUpload rule"
            if rules is not None and not _has_action(rules, "AbortIncompleteMultipartUpload")
            else ""
        )
        return self._rec(
            name, region, "s3-incomplete-multipart",
            f"{len(uploads)} incomplete multipart upload(s), oldest {oldest}d, "
            f"{size} of parts{scope}{note} — billed but invisible in the console{fix}",
            quote.amount if quote else Decimal(0),
            quote,
        )

    def _versions_rec(self, name: str, region: str, versioning: str) -> Recommendation:
        return self._rec(
            name, region, "s3-noncurrent-versions",
            f"Versioning {versioning.lower()} with no NoncurrentVersionExpiration rule "
            "— every overwrite is kept and billed (size unknown: CloudWatch doesn't "
            "split current from noncurrent)",
            # no figure: nothing free says how much of the bucket is old versions
            Decimal(0),
        )

    def _cold_rec(
        self, name: str, region: str, rules: list[dict] | None
    ) -> Recommendation | None:
        if not self._tuning.allow_cloudwatch_metrics:
            return None  # GetMetricData bills per metric
        if rules is None or _has_action(rules, "Transitions"):
            return None  # unreadable rules: can't claim a transition is missing
        total = self._cold_bytes(name, region)
        if total is None:
            return None
        gib = total / _GIB
        if gib < Decimal(str(self._tuning.s3_cold_min_gb)):
            return None
        quote = pricing.s3_transition_quote(gib, "standard_ia", "standard", region)
        approx = ", at us-east-1 rates" if quote.approximate else ""
        return self._rec(
            name, region, "s3-standard-no-transition",
            f"{gib:.0f} GiB in Standard with no transition rule — Standard-IA would "
            f"save ~{quote.amount:.0f} USD/mo if the data is cold{approx}; clont "
            "can't see read frequency (s3 request metrics are billed)",
            quote.amount,
            quote,
        )

    def _no_lifecycle_rec(self, name: str, region: str) -> Recommendation:
        return self._rec(
            name, region, "s3-no-lifecycle",
            "No lifecycle rule covering the bucket — nothing expires old objects "
            "or aborts failed uploads",
            Decimal(0),
        )

    def _rec(
        self,
        name: str,
        region: str,
        kind: str,
        summary: str,
        saving: Decimal,
        quote: pricing.Quote | None = None,
    ) -> Recommendation:
        return Recommendation(
            cloud=str(Cloud.AWS),
            service="s3",
            kind=kind,
            resource=CloudResource(
                cloud=Cloud.AWS,
                service="s3",
                resource_id=name,
                region=region,
                alias=self._provider.alias,
            ),
            summary=summary,
            estimated_savings=Money(amount=saving, currency=_USD),
            priced_region=quote.region if quote else None,
            # no quote means no figure to qualify; approximate stays the default
            approximate=quote.approximate if quote else True,
        )


def _aware(value):
    """A naive timestamp is utc here; comparing it raw raises TypeError."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _human(size_bytes: Decimal) -> str:
    """Bytes as MiB/GiB/TiB — a 5 MiB upload printed as "0.0 GiB" says nothing."""
    for unit, scale in (("TiB", _GIB * 1024), ("GiB", _GIB), ("MiB", Decimal(1024**2))):
        if size_bytes >= scale:
            return f"{size_bytes / scale:.1f} {unit}"
    return f"{size_bytes / Decimal(1024):.0f} KiB"


def _covers_bucket(rule: dict) -> bool:
    """True when the rule applies to every object, not one prefix or tag.

    A rule scoped to `logs/` says nothing about the rest of the bucket, so
    treating it as coverage would hide the finding for everything else.
    """
    if rule.get("Prefix"):  # legacy top-level scoping
        return False
    flt = rule.get("Filter") or {}
    if not flt:
        return True
    if "And" in flt or flt.get("Tag") or flt.get("ObjectSizeGreaterThan") is not None:
        return False
    if flt.get("ObjectSizeLessThan") is not None:
        return False
    return not flt.get("Prefix")


def _has_action(rules: list[dict], action: str) -> bool:
    return any(rule.get(action) for rule in rules)
