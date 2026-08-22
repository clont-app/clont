"""Account spend read from the Cost and Usage Report in S3.

The free replacement for `ce:GetCostAndUsage`: AWS writes the same numbers to a
bucket you own, so a cycle costs a couple of S3 GETs instead of $0.01. Output is
the daily per-service `CostRecord` stream the spend/budget/forecast detectors
already consume.

Legacy CUR layout only — gzip csv, manifest at the billing-period root:

    s3://<bucket>/<prefix>/<report>/<YYYYMMDD-YYYYMMDD>/<report>-Manifest.json

The manifest names the data files, so the bucket is never listed and the role
needs nothing but `s3:GetObject`. Keys are derived, not discovered: a month
whose manifest isn't there yet is skipped, not an error.

The report is rewritten a few times a day, so re-reading it every 300s cycle
would be pure waste — parsed totals are cached for `refresh_minutes`.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from botocore.exceptions import ClientError

from clont.core.logging import get_logger
from clont.core.models import Cloud, Money, Period
from clont.core.registry import register
from clont.finops.models import CostRecord, Recommendation
from clont.providers.base import Provider

log = get_logger("clont.finops.aws.cur")

# legacy cur column, then the snake_case data-exports spelling of the same thing
_DAY = ("lineItem/UsageStartDate", "line_item_usage_start_date")
_COST = ("lineItem/UnblendedCost", "line_item_unblended_cost")
_CURRENCY = ("lineItem/CurrencyCode", "line_item_currency_code")
_KIND = ("lineItem/LineItemType", "line_item_line_item_type")
_ACCOUNT = ("lineItem/UsageAccountId", "line_item_usage_account_id")
# ProductName is the closest thing to a Cost Explorer service name; the product
# code is the fallback when the report doesn't carry it.
_SERVICE = (
    "product/ProductName",
    "product_product_name",
    "lineItem/ProductCode",
    "line_item_product_code",
)

_ABSENT = {"NoSuchKey", "NoSuchBucket", "404"}


@dataclass
class _Spend:
    """Daily per-service totals for whole billing periods."""

    totals: dict[tuple[date, str], Decimal] = field(
        default_factory=lambda: defaultdict(Decimal)
    )
    currency: str = "USD"


_cache: dict[str, tuple[float, _Spend]] = {}


def clear_cache() -> None:
    _cache.clear()


@register("finops", Cloud.AWS, "cur")
class CURCostCollector:
    cloud = Cloud.AWS
    service = "cur"
    # free (an s3 read) and already self-throttled by cur.refresh_minutes;
    # a 24h outer ttl would make that knob dead code and stale the digest
    collect_every_seconds = 3600

    def __init__(self, provider: Provider, tuning=None) -> None:
        self._provider = provider

    def collect(self, period: Period) -> list[CostRecord]:
        config = getattr(self._provider, "cur", None)
        if config is None:
            return []

        spend = _spend(self._provider, config, period)
        records: list[CostRecord] = []
        for (day, service), amount in sorted(spend.totals.items()):
            if not period.start <= day <= period.end:
                continue
            records.append(
                CostRecord(
                    cloud=str(Cloud.AWS),
                    service=service,
                    period=Period(start=day, end=day),
                    alias=self._provider.alias,
                    cost=Money(amount=amount, currency=spend.currency),
                )
            )
        return records

    def recommendations(self, period: Period) -> list[Recommendation]:
        return []


def _spend(provider: Provider, config, period: Period) -> _Spend:
    folders = _billing_periods(period)
    key = "|".join([str(provider.alias), config.bucket, config.prefix, config.report_name, *folders])
    hit = _cache.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < config.refresh_minutes * 60:
        return hit[1]

    s3 = provider.client("s3", config.region)
    account = None if config.include_linked else getattr(provider, "account_id", None)
    spend = _Spend()
    for folder in folders:
        manifest = _manifest(s3, config, folder)
        if manifest is None:
            log.warning(
                "no CUR manifest for %s in s3://%s — spend for that period is missing",
                folder,
                config.bucket,
            )
            continue
        _check_format(manifest, folder)
        for data_key in manifest.get("reportKeys", []):
            _read_into(s3, config.bucket, data_key, account, spend)

    # only the current window is ever asked for; don't accumulate old months
    _cache.clear()
    _cache[key] = (now, spend)
    return spend


def read_manifest(s3, config, day: date) -> dict | None:
    """The manifest covering `day`, or None if AWS hasn't delivered it yet."""
    return _manifest(s3, config, _folder(day.replace(day=1)))


def _billing_periods(period: Period) -> list[str]:
    """Folder names of every billing period the window touches."""
    folders: list[str] = []
    month = period.start.replace(day=1)
    while month <= period.end:
        folders.append(_folder(month))
        month = _next_month(month)
    return folders


def _folder(month: date) -> str:
    return f"{month:%Y%m%d}-{_next_month(month):%Y%m%d}"


def _next_month(month: date) -> date:
    return (month.replace(day=28) + timedelta(days=4)).replace(day=1)


def _manifest(s3, config, folder: str) -> dict | None:
    parts = [config.prefix.strip("/"), config.report_name, folder, f"{config.report_name}-Manifest.json"]
    key = "/".join(p for p in parts if p)
    try:
        body = s3.get_object(Bucket=config.bucket, Key=key)["Body"]
    except ClientError as exc:
        # a period AWS hasn't delivered yet is normal; anything else (denied,
        # wrong bucket region) is the operator's to fix, so let it surface
        if str(exc.response.get("Error", {}).get("Code")) in _ABSENT:
            return None
        raise
    return json.loads(body.read())


def _check_format(manifest: dict, folder: str) -> None:
    compression = str(manifest.get("compression", "GZIP")).upper()
    content = str(manifest.get("contentType", "text/csv")).lower()
    if compression != "GZIP" or "csv" not in content:
        raise RuntimeError(
            f"CUR {folder} is {compression}/{content}; clont reads gzip csv only "
            "(recreate the report with GZIP + text/csv)"
        )


def _read_into(s3, bucket: str, key: str, account: str | None, spend: _Spend) -> None:
    """Stream one gzipped csv part, folding its rows into `spend`."""
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    with gzip.GzipFile(fileobj=body) as gz:
        for row in csv.DictReader(io.TextIOWrapper(gz, encoding="utf-8")):
            if account is not None:
                owner = _pick(row, _ACCOUNT)
                if owner and owner != account:
                    continue
            day = _day(row)
            amount = _amount(row)
            if day is None or not amount:
                continue
            spend.totals[(day, _service(row))] += amount
            currency = _pick(row, _CURRENCY)
            if currency and currency != spend.currency:
                spend.currency = currency


def _pick(row: dict, names: tuple[str, ...]) -> str:
    for name in names:
        value = row.get(name)
        if value:
            return value.strip()
    return ""


def _day(row: dict) -> date | None:
    stamp = _pick(row, _DAY)
    try:
        return date.fromisoformat(stamp[:10])
    except ValueError:
        return None


def _amount(row: dict) -> Decimal:
    try:
        return Decimal(_pick(row, _COST) or "0")
    except InvalidOperation:
        return Decimal(0)


def _service(row: dict) -> str:
    # tax/credit/refund lines carry no product, but their line-item type is
    # exactly how Cost Explorer labels them
    return _pick(row, _SERVICE) or _pick(row, _KIND) or "unknown"
