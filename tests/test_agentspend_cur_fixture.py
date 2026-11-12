"""Agent spend off real cur 2.0 rows.

The fixture is a scrubbed copy of the rows aws billed for a live nova micro
run (account and profile ids faked). `buckets` is what bedrock itself counted,
in tokens, so the bill and the model meter check each other here.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from clont.core.config import CURConfig
from clont.core.models import Period
from clont.finops.agentspend import agent_report
from clont.finops.aws import cur
from clont.finops.aws.ai_usage_types import agent_bucket
from clont.finops.base import FinOpsTuning

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "cur2_agentspend.json").read_text())
ROWS = FIXTURE["rows"]
TAG = FIXTURE["tag_key"]
BUCKET = "billing-bucket"
EXPORT = "clont-cur"


class _S3:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects

    def get_object(self, Bucket: str, Key: str):
        if Key not in self._objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        return {"Body": io.BytesIO(self._objects[Key])}


class _Provider:
    alias = "lab"
    account_id = ROWS[0]["line_item_usage_account_id"]
    organizations = None

    def __init__(self) -> None:
        self.cur = CURConfig(bucket=BUCKET, report_name=EXPORT)
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(ROWS[0]))
        writer.writeheader()
        writer.writerows(ROWS)
        data = f"{EXPORT}/data/BILLING_PERIOD=2026-11/part-0.csv.gz"
        manifest = {"dataFiles": [f"s3://{BUCKET}/{data}"]}
        self._s3 = _S3(
            {
                f"{EXPORT}/metadata/BILLING_PERIOD=2026-11/{EXPORT}-Manifest.json": json.dumps(
                    manifest
                ).encode(),
                data: gzip.compress(out.getvalue().encode()),
            }
        )

    def client(self, service: str, region: str | None = None):
        assert service == "s3"
        return self._s3


@pytest.fixture(autouse=True)
def _no_cache():
    cur.clear_cache()
    yield
    cur.clear_cache()


def _records():
    tuning = FinOpsTuning(agent_tag_keys=(TAG,))
    period = Period(start=date(2026, 11, 1), end=date(2026, 11, 30))
    return cur.CURCostCollector(_Provider(), tuning).collect(period)


def test_fixture_is_scrubbed():
    blob = json.dumps(ROWS)
    assert "123456789012" in blob and "fakeprofile0001" in blob
    accounts = {r["line_item_usage_account_id"] for r in ROWS}
    assert accounts == {"123456789012"}


def test_each_usage_type_lands_in_the_bucket_bedrock_counted():
    tokens: dict[str, Decimal] = {}
    for row in ROWS:
        bucket = agent_bucket(row["line_item_product_code"], row["line_item_usage_type"], True)
        # cur meters bedrock in 1k tokens
        tokens[bucket] = tokens.get(bucket, Decimal(0)) + Decimal(row["line_item_usage_amount"]) * 1000

    assert tokens == {k: Decimal(v) for k, v in FIXTURE["buckets"].items()}


def test_collector_keeps_every_billed_cent_under_the_agent_tag():
    records = _records()

    assert records
    assert {(r.tags or {}).get(TAG) for r in records} == {FIXTURE["agent"]}
    got: dict[str, Decimal] = {}
    for r in records:
        bucket = r.dimensions["agent"]
        got[bucket] = got.get(bucket, Decimal(0)) + r.cost.amount
    want: dict[str, Decimal] = {}
    for row in ROWS:
        cost = Decimal(row["line_item_unblended_cost"])
        if cost:  # zero-cost rows never reach a record
            bucket = agent_bucket(row["line_item_product_code"], row["line_item_usage_type"], True)
            want[bucket] = want.get(bucket, Decimal(0)) + cost
    assert got == want
    assert sum(got.values()) == sum(Decimal(r["line_item_unblended_cost"]) for r in ROWS)


def test_agent_report_reads_it_as_pure_token_spend():
    (report,) = agent_report(_records(), (TAG,))

    assert report.agent == FIXTURE["agent"]
    assert report.currency == "USD"
    assert (report.start, report.end) == (date(2026, 11, 10), date(2026, 11, 10))
    assert report.floor == report.plumbing == Decimal("0.00")
    assert report.monthly_floor_run_rate == Decimal("0.00")
    assert report.tokens == report.total
    assert {line.bucket for line in report.lines} == {"cache-read", "tokens-in", "tokens-out"}
    # the run was built to hit the cache: 99% of input-side money is cache reads
    assert report.cache_read_share == Decimal("99.2")
