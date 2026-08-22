"""The CUR collector — the free replacement for ce:GetCostAndUsage.

The fake S3 is a dict of key -> bytes that counts reads, because two of the
properties that matter here are about *not* fetching: the manifest is derived
rather than listed, and parsed totals are cached across cycles.
"""

from __future__ import annotations

import gzip
import io
import json
from datetime import date
from decimal import Decimal

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from clont.core.config import CURConfig
from clont.core.models import Period
from clont.finops.aws import cur

BUCKET = "billing-bucket"
JAN = "20240101-20240201"
FEB = "20240201-20240301"


def _gz(rows: list[dict]) -> bytes:
    header = [
        "lineItem/UsageStartDate",
        "lineItem/UnblendedCost",
        "lineItem/CurrencyCode",
        "lineItem/LineItemType",
        "lineItem/UsageAccountId",
        "product/ProductName",
    ]
    out = io.StringIO()
    out.write(",".join(header) + "\n")
    for row in rows:
        out.write(",".join(str(row.get(c, "")) for c in header) + "\n")
    return gzip.compress(out.getvalue().encode())


def _row(day: str, cost: str, service: str, *, account: str = "111", kind: str = "Usage") -> dict:
    return {
        "lineItem/UsageStartDate": f"{day}T00:00:00Z",
        "lineItem/UnblendedCost": cost,
        "lineItem/CurrencyCode": "USD",
        "lineItem/LineItemType": kind,
        "lineItem/UsageAccountId": account,
        "product/ProductName": service,
    }


def _manifest(*keys: str) -> bytes:
    return json.dumps(
        {"compression": "GZIP", "contentType": "text/csv", "reportKeys": list(keys)}
    ).encode()


class _FakeS3:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects
        self.reads: list[str] = []

    def get_object(self, Bucket: str, Key: str):  # noqa: N803 - boto3 spelling
        assert Bucket == BUCKET
        self.reads.append(Key)
        try:
            body = self._objects[Key]
        except KeyError:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}}, "GetObject"
            ) from None
        return {"Body": io.BytesIO(body)}


class _FakeProvider:
    def __init__(self, s3: _FakeS3, *, cur_config=None, account_id: str | None = "111") -> None:
        self._s3 = s3
        self.alias = "prod"
        self.account_id = account_id
        self.cur = cur_config if cur_config is not None else _config()

    def client(self, service: str, region: str | None = None):
        assert service == "s3"
        return self._s3


def _config(**kw) -> CURConfig:
    return CURConfig(bucket=BUCKET, report_name="clont-cur", prefix="reports", **kw)


def _objects(**kw) -> dict[str, bytes]:
    return {
        f"reports/clont-cur/{JAN}/clont-cur-Manifest.json": _manifest("reports/clont-cur/data-1.csv.gz"),
        "reports/clont-cur/data-1.csv.gz": _gz(
            [
                _row("2024-01-01", "1.50", "Amazon Elastic Compute Cloud"),
                _row("2024-01-01", "0.50", "Amazon Elastic Compute Cloud"),
                _row("2024-01-01", "2.00", "Amazon Simple Storage Service"),
                _row("2024-01-02", "3.00", "Amazon Elastic Compute Cloud"),
            ]
            + kw.get("extra", [])
        ),
    }


@pytest.fixture(autouse=True)
def _no_cache():
    cur.clear_cache()
    yield
    cur.clear_cache()


def _collect(provider, period: Period | None = None):
    period = period or Period(start=date(2024, 1, 1), end=date(2024, 1, 31))
    return cur.CURCostCollector(provider).collect(period)


def test_rows_fold_into_daily_per_service_records():
    records = _collect(_FakeProvider(_FakeS3(_objects())))

    assert [(r.period.start, r.service, r.cost.amount) for r in records] == [
        (date(2024, 1, 1), "Amazon Elastic Compute Cloud", Decimal("2.00")),
        (date(2024, 1, 1), "Amazon Simple Storage Service", Decimal("2.00")),
        (date(2024, 1, 2), "Amazon Elastic Compute Cloud", Decimal("3.00")),
    ]
    assert {r.alias for r in records} == {"prod"}
    assert {r.cost.currency for r in records} == {"USD"}


def test_records_outside_the_window_are_dropped():
    records = _collect(
        _FakeProvider(_FakeS3(_objects())),
        Period(start=date(2024, 1, 2), end=date(2024, 1, 2)),
    )

    assert [r.period.start for r in records] == [date(2024, 1, 2)]


def test_linked_account_rows_are_skipped_by_default():
    objects = _objects(extra=[_row("2024-01-01", "99.00", "Amazon RDS", account="999")])

    records = _collect(_FakeProvider(_FakeS3(objects)))

    assert "Amazon RDS" not in {r.service for r in records}


def test_include_linked_keeps_the_whole_payer_report():
    objects = _objects(extra=[_row("2024-01-01", "99.00", "Amazon RDS", account="999")])
    provider = _FakeProvider(_FakeS3(objects), cur_config=_config(include_linked=True))

    records = _collect(provider)

    assert ("Amazon RDS", Decimal("99.00")) in [(r.service, r.cost.amount) for r in records]


def test_tax_lines_fall_back_to_their_line_item_type():
    objects = _objects(extra=[_row("2024-01-01", "0.40", "", kind="Tax")])

    records = _collect(_FakeProvider(_FakeS3(objects)))

    assert (date(2024, 1, 1), "Tax", Decimal("0.40")) in [
        (r.period.start, r.service, r.cost.amount) for r in records
    ]


def test_a_window_spanning_two_months_reads_both_manifests():
    objects = _objects()
    objects[f"reports/clont-cur/{FEB}/clont-cur-Manifest.json"] = _manifest(
        "reports/clont-cur/data-feb.csv.gz"
    )
    objects["reports/clont-cur/data-feb.csv.gz"] = _gz(
        [_row("2024-02-01", "7.00", "Amazon Elastic Compute Cloud")]
    )
    s3 = _FakeS3(objects)

    records = _collect(
        _FakeProvider(s3), Period(start=date(2024, 1, 30), end=date(2024, 2, 1))
    )

    # january's rows are all before the window, february's is inside it
    assert [(r.period.start, r.cost.amount) for r in records] == [
        (date(2024, 2, 1), Decimal("7.00"))
    ]
    assert f"reports/clont-cur/{FEB}/clont-cur-Manifest.json" in s3.reads


def test_missing_manifest_is_not_an_error():
    assert _collect(_FakeProvider(_FakeS3({}))) == []


def test_denied_manifest_read_surfaces():
    class _Denied(_FakeS3):
        def get_object(self, Bucket: str, Key: str):  # noqa: N803
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    with pytest.raises(ClientError):
        _collect(_FakeProvider(_Denied({})))


def test_non_csv_report_fails_loudly():
    objects = _objects()
    objects[f"reports/clont-cur/{JAN}/clont-cur-Manifest.json"] = json.dumps(
        {"compression": "Parquet", "contentType": "application/parquet", "reportKeys": []}
    ).encode()

    with pytest.raises(RuntimeError, match="gzip csv only"):
        _collect(_FakeProvider(_FakeS3(objects)))


def test_second_cycle_serves_the_cache():
    s3 = _FakeS3(_objects())
    provider = _FakeProvider(s3)

    first = _collect(provider)
    reads = len(s3.reads)
    second = _collect(provider)

    assert first == second
    assert len(s3.reads) == reads  # no second trip to S3


def test_cache_expires_after_refresh_minutes(monkeypatch):
    s3 = _FakeS3(_objects())
    provider = _FakeProvider(s3, cur_config=_config(refresh_minutes=1))
    clock = [1000.0]
    monkeypatch.setattr(cur.time, "monotonic", lambda: clock[0])

    _collect(provider)
    reads = len(s3.reads)
    clock[0] += 61
    _collect(provider)

    assert len(s3.reads) > reads


def test_no_cur_configured_means_no_records():
    provider = _FakeProvider(_FakeS3({}))
    provider.cur = None

    assert _collect(provider) == []


def test_only_the_manifest_and_its_data_files_are_fetched():
    # keys are derived from the billing period, so the role needs no ListBucket
    s3 = _FakeS3(_objects())

    _collect(_FakeProvider(s3))

    assert s3.reads == [
        f"reports/clont-cur/{JAN}/clont-cur-Manifest.json",
        "reports/clont-cur/data-1.csv.gz",
    ]


# --- moto: the real botocore streaming body, not our BytesIO ---------------


@pytest.fixture
def aws_creds(monkeypatch):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@mock_aws
def test_reads_a_real_gzipped_object(aws_creds):
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=BUCKET)
    manifest_key = f"reports/clont-cur/{JAN}/clont-cur-Manifest.json"
    data_key = f"reports/clont-cur/{JAN}/assembly/clont-cur-1.csv.gz"
    s3.put_object(Bucket=BUCKET, Key=manifest_key, Body=_manifest(data_key))
    s3.put_object(
        Bucket=BUCKET,
        Key=data_key,
        # two hourly rows of the same day fold into one record
        Body=_gz(
            [
                _row("2024-01-05", "1.25", "Amazon Elastic Compute Cloud"),
                _row("2024-01-05", "0.75", "Amazon Elastic Compute Cloud"),
            ]
        ),
    )

    class _Provider(_FakeProvider):
        def client(self, service: str, region: str | None = None):
            return s3

    records = _collect(_Provider(None))

    assert [(r.period.start, r.service, r.cost.amount) for r in records] == [
        (date(2024, 1, 5), "Amazon Elastic Compute Cloud", Decimal("2.00")),
    ]
