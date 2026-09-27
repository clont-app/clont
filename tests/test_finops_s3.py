"""S3 storage waste: lifecycle rules, noncurrent versions, abandoned uploads.

Pins the things that would quietly make the report wrong: a prefix-scoped rule
counting as coverage, a bucket getting four rows for one problem, and the
cold-data check firing without the paid metrics flag.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from botocore.exceptions import ClientError

from clont.finops.aws.s3 import S3StorageCollector
from clont.finops.base import FinOpsTuning

_NOW = datetime.now(UTC)


class _Paginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **kw):
        yield from self._pages


class _FakeS3:
    def __init__(
        self,
        buckets: list[str],
        rules: list[dict] | None = None,
        versioning: str = "",
        uploads: list[dict] | None = None,
        parts: dict[str, list[dict]] | None = None,
    ) -> None:
        self._buckets = buckets
        self._rules = rules
        self._versioning = versioning
        self._uploads = uploads or []
        self._parts = parts or {}
        self.listed_parts: list[str] = []

    def list_buckets(self):
        return {"Buckets": [{"Name": b} for b in self._buckets]}

    def get_bucket_location(self, Bucket: str):  # noqa: N803 - boto3 spelling
        return {"LocationConstraint": "eu-west-1"}

    def get_bucket_lifecycle_configuration(self, Bucket: str):  # noqa: N803
        if self._rules is None:
            raise ClientError(
                {"Error": {"Code": "NoSuchLifecycleConfiguration"}},
                "GetBucketLifecycleConfiguration",
            )
        return {"Rules": self._rules}

    def get_bucket_versioning(self, Bucket: str):  # noqa: N803
        return {"Status": self._versioning} if self._versioning else {}

    def get_paginator(self, name: str):
        if name == "list_multipart_uploads":
            return _Paginator([{"Uploads": self._uploads}])
        assert name == "list_parts"
        outer = self

        class _P(_Paginator):
            def paginate(self, **kw):
                outer.listed_parts.append(kw["UploadId"])
                yield {"Parts": outer._parts.get(kw["UploadId"], [])}

        return _P([])


class _FakeCloudWatch:
    def __init__(self, standard_bytes: float | None) -> None:
        self._bytes = standard_bytes
        self.queries: list[dict] = []

    def get_metric_data(self, **kw):
        self.queries.extend(kw["MetricDataQueries"])
        if self._bytes is None:
            return {"MetricDataResults": [{"Id": "std", "Timestamps": [], "Values": []}]}
        return {
            "MetricDataResults": [
                {
                    "Id": "std",
                    # newest value is not last: the collector must pick by timestamp
                    "Timestamps": [_NOW, _NOW - timedelta(days=1)],
                    "Values": [self._bytes, self._bytes * 2],
                }
            ]
        }


class _FakeProvider:
    def __init__(self, s3: _FakeS3, cw: _FakeCloudWatch | None = None) -> None:
        self._s3 = s3
        self._cw = cw
        self.alias = "prod"
        self.regions_asked: list[str] = []

    def regions(self) -> list[str]:
        return ["us-east-1"]

    def client(self, service: str, region: str | None = None):
        self.regions_asked.append(f"{service}:{region}")
        if service == "cloudwatch":
            assert self._cw is not None
            return self._cw
        assert service == "s3"
        return self._s3


def _upload(uid: str, days: int, storage_class: str = "STANDARD") -> dict:
    return {
        "Key": f"k/{uid}",
        "UploadId": uid,
        "Initiated": _NOW - timedelta(days=days),
        "StorageClass": storage_class,
    }


def _run(s3: _FakeS3, cw: _FakeCloudWatch | None = None, **tuning):
    provider = _FakeProvider(s3, cw)
    collector = S3StorageCollector(provider, FinOpsTuning(**tuning))
    return collector.recommendations(None), provider


def _kinds(recs) -> list[str]:
    return [r.kind for r in recs]


# --- lifecycle --------------------------------------------------------------


def test_a_bucket_with_no_lifecycle_is_flagged_once():
    recs, _ = _run(_FakeS3(["logs"]))

    assert _kinds(recs) == ["s3-no-lifecycle"]
    assert recs[0].resource.resource_id == "logs"
    assert recs[0].resource.region == "eu-west-1"  # the bucket's own region, not us-east-1
    assert recs[0].estimated_savings.amount == Decimal(0)


def test_an_unscoped_expiration_rule_is_coverage():
    recs, _ = _run(_FakeS3(["logs"], rules=[{"Status": "Enabled", "Expiration": {"Days": 30}}]))

    assert recs == []


def test_a_prefix_scoped_rule_is_not_coverage():
    # a rule on logs/ says nothing about the rest of the bucket
    recs, _ = _run(
        _FakeS3(
            ["logs"],
            rules=[
                {
                    "Status": "Enabled",
                    "Filter": {"Prefix": "logs/"},
                    "NoncurrentVersionExpiration": {"NoncurrentDays": 7},
                }
            ],
            versioning="Enabled",
        )
    )

    assert _kinds(recs) == ["s3-noncurrent-versions"]


def test_a_disabled_rule_is_not_coverage():
    recs, _ = _run(_FakeS3(["logs"], rules=[{"Status": "Disabled", "Expiration": {"Days": 1}}]))

    assert _kinds(recs) == ["s3-no-lifecycle"]


# --- versioning -------------------------------------------------------------


def test_versioning_without_an_expiration_rule_is_flagged():
    recs, _ = _run(_FakeS3(["data"], versioning="Enabled"))

    # the generic no-lifecycle row is suppressed: we have something specific
    assert _kinds(recs) == ["s3-noncurrent-versions"]
    assert "no NoncurrentVersionExpiration" in recs[0].summary
    # nothing free says how much of the bucket is old versions
    assert recs[0].estimated_savings.amount == Decimal(0)


def test_suspended_versioning_still_keeps_the_old_versions():
    recs, _ = _run(_FakeS3(["data"], versioning="Suspended"))

    assert _kinds(recs) == ["s3-noncurrent-versions"]


def test_an_unversioned_bucket_is_not_asked_about_versions():
    recs, _ = _run(_FakeS3(["data"], rules=[{"Status": "Enabled", "Expiration": {"Days": 30}}]))

    assert recs == []


def test_a_noncurrent_expiration_rule_clears_it():
    recs, _ = _run(
        _FakeS3(
            ["data"],
            rules=[{"Status": "Enabled", "NoncurrentVersionExpiration": {"NoncurrentDays": 30}}],
            versioning="Enabled",
        )
    )

    assert recs == []


# --- incomplete multipart uploads -------------------------------------------


def test_old_uploads_are_sized_and_priced():
    s3 = _FakeS3(
        ["big"],
        uploads=[_upload("u1", days=30)],
        parts={"u1": [{"Size": 512 * 1024**2}, {"Size": 512 * 1024**2}]},
    )
    recs, _ = _run(s3)

    assert _kinds(recs) == ["s3-incomplete-multipart"]
    rec = recs[0]
    assert "1 incomplete multipart upload(s), oldest 30d, 1.0 GiB" in rec.summary
    assert "AbortIncompleteMultipartUpload" in rec.summary
    # 1 GiB of standard storage in eu-west-1, a real charge with a real figure
    assert rec.estimated_savings.amount > Decimal(0)


def test_a_fresh_upload_is_not_abandoned():
    s3 = _FakeS3(["big"], uploads=[_upload("u1", days=1)])
    recs, _ = _run(s3, s3_multipart_min_age_days=7)

    assert _kinds(recs) == ["s3-no-lifecycle"]
    assert s3.listed_parts == []  # nothing to size, don't pay for the calls


def test_the_abort_rule_is_not_mentioned_when_it_exists():
    s3 = _FakeS3(
        ["big"],
        rules=[{"Status": "Enabled", "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}],
        uploads=[_upload("u1", days=30)],
        parts={"u1": [{"Size": 1024**3}]},
    )
    recs, _ = _run(s3)

    # the parts are billed right now whatever the rule says, so it still reports
    assert _kinds(recs) == ["s3-incomplete-multipart"]
    assert "add an AbortIncompleteMultipartUpload" not in recs[0].summary


def test_only_the_oldest_uploads_are_sized_and_the_count_is_honest():
    uploads = [_upload(f"u{i}", days=10 + i) for i in range(30)]
    parts = {u["UploadId"]: [{"Size": 1024**3}] for u in uploads}
    s3 = _FakeS3(["big"], uploads=uploads, parts=parts)
    recs, _ = _run(s3)

    assert len(s3.listed_parts) == 25  # list_parts is a call per upload
    assert "30 incomplete multipart upload(s)" in recs[0].summary
    assert "sized the oldest 25" in recs[0].summary
    # oldest first: the 39d upload, not the 10d one
    assert "oldest 39d" in recs[0].summary


# --- cold data in standard --------------------------------------------------


def test_cold_standard_needs_the_metrics_flag():
    cw = _FakeCloudWatch(200 * 1024**3)
    recs, _ = _run(_FakeS3(["cold"], rules=[{"Status": "Enabled", "Expiration": {"Days": 3650}}]), cw)

    assert recs == []
    assert cw.queries == []  # GetMetricData bills per metric requested


def test_standard_bytes_with_no_transition_rule_is_flagged():
    cw = _FakeCloudWatch(200 * 1024**3)
    recs, _ = _run(
        _FakeS3(["cold"], rules=[{"Status": "Enabled", "Expiration": {"Days": 3650}}]),
        cw,
        allow_cloudwatch_metrics=True,
    )

    assert _kinds(recs) == ["s3-standard-no-transition"]
    assert "200 GiB in Standard" in recs[0].summary
    assert "if the data is cold" in recs[0].summary  # read frequency is not free
    assert recs[0].estimated_savings.amount > Decimal(0)
    # both dimensions, or the query matches nothing
    dims = cw.queries[0]["MetricStat"]["Metric"]["Dimensions"]
    assert {d["Name"] for d in dims} == {"BucketName", "StorageType"}


def test_a_transition_rule_skips_the_metric_call_entirely():
    cw = _FakeCloudWatch(200 * 1024**3)
    recs, _ = _run(
        _FakeS3(
            ["cold"],
            rules=[{"Status": "Enabled", "Transitions": [{"StorageClass": "GLACIER"}]}],
        ),
        cw,
        allow_cloudwatch_metrics=True,
    )

    assert recs == []
    assert cw.queries == []


def test_a_small_bucket_is_not_worth_a_transition():
    cw = _FakeCloudWatch(5 * 1024**3)
    recs, _ = _run(
        _FakeS3(["small"], rules=[{"Status": "Enabled", "Expiration": {"Days": 3650}}]),
        cw,
        allow_cloudwatch_metrics=True,
        s3_cold_min_gb=100.0,
    )

    assert recs == []


def test_no_datapoints_means_no_claim():
    cw = _FakeCloudWatch(None)
    recs, _ = _run(
        _FakeS3(["cold"], rules=[{"Status": "Enabled", "Expiration": {"Days": 3650}}]),
        cw,
        allow_cloudwatch_metrics=True,
    )

    assert recs == []


def test_the_metric_is_read_in_the_buckets_own_region():
    cw = _FakeCloudWatch(200 * 1024**3)
    _, provider = _run(
        _FakeS3(["cold"], rules=[{"Status": "Enabled", "Expiration": {"Days": 3650}}]),
        cw,
        allow_cloudwatch_metrics=True,
    )

    assert "cloudwatch:eu-west-1" in provider.regions_asked


# --- failure isolation ------------------------------------------------------


def test_one_denied_bucket_does_not_sink_the_report():
    class _Denied(_FakeS3):
        def get_bucket_versioning(self, Bucket: str):  # noqa: N803
            if Bucket == "locked":
                raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetBucketVersioning")
            return {}

    recs, _ = _run(_Denied(["locked", "open"]))

    assert [r.resource.resource_id for r in recs] == ["open"]
