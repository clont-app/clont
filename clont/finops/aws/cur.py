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

A payer's report (`include_linked`) is grouped by `lineItem/UsageAccountId`, and
each linked account's records carry that account's own alias — so the digest,
spike, forecast, budget and showback detectors all work per account without
knowing anything about organizations.

Costs are **amortized** by default: an all-upfront RI or a savings plan fee is
one huge unblended row on the day it is bought, which reads as a spike, wrecks
the forecast and blows a budget for a month that didn't actually cost that. So
the effective-cost columns are used instead — covered usage is priced at what
the commitment makes it cost, and only the *unused* part of a fee is charged.

Credits, refunds and tax are not usage: a credit landing on EC2 makes EC2 look
like it got cheaper for a day. They keep their own service bucket (the names
Cost Explorer uses) so the totals still net out while no service's trend moves.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from botocore.exceptions import ClientError

from clont.core.logging import get_logger
from clont.core.models import Cloud, Money, Period
from clont.core.registry import register
from clont.finops.aws.dynamodb import capacity_mode_recommendations, throughput_kind
from clont.finops.aws.usage_types import DIMENSION as _TRANSFER
from clont.finops.aws.usage_types import transfer_bucket
from clont.finops.base import FinOpsTuning
from clont.finops.models import CostRecord, Recommendation
from clont.providers.aws.organizations import account_names
from clont.providers.base import Provider

log = get_logger("clont.finops.aws.cur")

# legacy cur column, then the snake_case data-exports spelling of the same thing
_DAY = ("lineItem/UsageStartDate", "line_item_usage_start_date")
_COST = ("lineItem/UnblendedCost", "line_item_unblended_cost")
_CURRENCY = ("lineItem/CurrencyCode", "line_item_currency_code")
_KIND = ("lineItem/LineItemType", "line_item_line_item_type")
_ACCOUNT = ("lineItem/UsageAccountId", "line_item_usage_account_id")
_USAGE_TYPE = ("lineItem/UsageType", "line_item_usage_type")
# ProductName is the closest thing to a Cost Explorer service name; the product
# code is the fallback when the report doesn't carry it.
_SERVICE = (
    "product/ProductName",
    "product_product_name",
    "lineItem/ProductCode",
    "line_item_product_code",
)

_USAGE_AMOUNT = ("lineItem/UsageAmount", "line_item_usage_amount")
_RESOURCE = ("lineItem/ResourceId", "line_item_resource_id")

# amortization columns: what a covered hour really costs, and the slice of a
# commitment nobody used. legacy cur first, then the data-exports spelling
_RI_EFFECTIVE = ("reservation/EffectiveCost", "reservation_effective_cost")
_RI_UNUSED_UPFRONT = (
    "reservation/UnusedAmortizedUpfrontFeeForBillingPeriod",
    "reservation_unused_amortized_upfront_fee_for_billing_period",
)
_RI_UNUSED_RECURRING = ("reservation/UnusedRecurringFee", "reservation_unused_recurring_fee")
_RI_ARN = ("reservation/ReservationARN", "reservation_reservation_a_r_n")
_SP_EFFECTIVE = (
    "savingsPlan/SavingsPlanEffectiveCost",
    "savings_plan_savings_plan_effective_cost",
)
_SP_TOTAL_COMMITMENT = (
    "savingsPlan/TotalCommitmentToDate",
    "savings_plan_total_commitment_to_date",
)
_SP_USED_COMMITMENT = ("savingsPlan/UsedCommitment", "savings_plan_used_commitment")

# a report either carries these or it doesn't; without them nothing is amortized
_RI_AMORTIZED = (_RI_EFFECTIVE, _RI_UNUSED_UPFRONT, _RI_UNUSED_RECURRING)
_SP_AMORTIZED = (_SP_EFFECTIVE, _SP_TOTAL_COMMITMENT, _SP_USED_COMMITMENT)

# charge types that move the bill without saying anything about consumption,
# value = the service bucket they land in, spelled as cost explorer does
_ADJUSTMENTS = {"Credit": "Credit", "Refund": "Refund", "Tax": "Tax"}

_ABSENT = {"NoSuchKey", "NoSuchBucket", "404"}

# dynamodb throughput rows are also kept per table and per report bucket, because
# the capacity-mode check needs the usage *amount*, not just the money. a cap
# keeps a huge payer report from turning that into a memory problem: the advice
# is dropped rather than computed from half the rows.
_MAX_DDB_KEYS = 200_000
# bucket start, usage account (empty when the report isn't split), table arn, kind
_DdbGroup = tuple[datetime, str, str, str]

# user cost-allocation tag columns: legacy, then the data-exports spelling
_TAG_PREFIXES = ("resourceTags/user:", "resource_tags_user_")
# splitting by tag multiplies the rows: days x services x distinct combos. a
# high-cardinality required tag (Name, say) would otherwise eat the box, so
# surplus combos fold into one labelled bucket instead of being dropped.
# the cap counts *tag combos*, not groups: days x accounts x services alone can
# pass 5000 on a big payer, and capping that would blank the tags of a report
# whose cardinality is fine
_MAX_TAG_COMBOS = 5000
_OTHER = "(other)"

# day, usage account ("" when the report is not split by account), service,
# data-transfer bucket ("" for everything that isn't transfer), tags
_Group = tuple[date, str, str, str, tuple[tuple[str, str], ...]]


@dataclass
class _Spend:
    """Daily per-account per-service totals, split by transfer bucket and tag combo."""

    totals: dict[_Group, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    currency: str = "USD"
    seen_tags: set[str] = field(default_factory=set)  # keys the report actually carries
    combos: set[tuple[tuple[str, str], ...]] = field(default_factory=set)
    ddb: dict[_DdbGroup, tuple[Decimal, Decimal]] = field(default_factory=dict)
    ddb_capped: bool = False     # hit _MAX_DDB_KEYS, so the usage is incomplete
    ddb_no_ids: bool = False      # dynamodb rows with no resource id column

    def add_ddb(
        self, start: datetime, account: str, table: str, kind: str, usage: Decimal, cost: Decimal
    ) -> None:
        key = (start, account, table, kind)
        if key not in self.ddb and len(self.ddb) >= _MAX_DDB_KEYS:
            self.ddb_capped = True
            return
        seen_usage, seen_cost = self.ddb.get(key, (Decimal(0), Decimal(0)))
        self.ddb[key] = (seen_usage + usage, seen_cost + cost)

    def add(
        self,
        day: date,
        account: str,
        service: str,
        transfer: str,
        tags: tuple[tuple[str, str], ...],
        amount: Decimal,
    ) -> None:
        if tags and tags not in self.combos:
            if len(self.combos) >= _MAX_TAG_COMBOS:
                tags = tuple((k, _OTHER) for k, _ in tags)
            else:
                self.combos.add(tags)
        self.totals[(day, account, service, transfer, tags)] += amount


@dataclass(frozen=True)
class _Charges:
    """How non-usage line types are counted, straight off the cur config."""

    amortize: bool = True
    credits: bool = True
    tax: bool = True

    @classmethod
    def of(cls, config) -> _Charges:
        return cls(config.amortize, config.include_credits, config.include_tax)

    def keeps(self, charge: str) -> bool:
        """Whether an adjustment row is counted — a refund rides with credits."""
        return self.tax if charge == "Tax" else self.credits


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
        self._tuning = tuning or FinOpsTuning()
        # the showback keys. splitting costs nothing downstream: every line is
        # still counted once, so service totals are unchanged
        self._tags = tuple(self._tuning.required_tags)

    def collect(self, period: Period) -> list[CostRecord]:
        config = getattr(self._provider, "cur", None)
        if config is None:
            return []

        spend = _spend(self._provider, config, period, self._tags)
        names = _linked_names(self._provider, config, spend)
        records: list[CostRecord] = []
        for (day, account, service, transfer, tags), amount in sorted(spend.totals.items()):
            if not period.start <= day <= period.end:
                continue
            dimensions = {}
            if account:
                dimensions["account_id"] = account
            if transfer:
                dimensions[_TRANSFER] = transfer
            records.append(
                CostRecord(
                    cloud=str(Cloud.AWS),
                    service=service,
                    period=Period(start=day, end=day),
                    alias=names.get(account, self._provider.alias),
                    cost=Money(amount=amount, currency=spend.currency),
                    dimensions=dimensions or None,
                    tags=dict(tags) if tags else None,
                )
            )
        return records

    def recommendations(self, period: Period) -> list[Recommendation]:
        """DynamoDB billing-mode advice, derived from the report already parsed."""
        config = getattr(self._provider, "cur", None)
        if config is None:
            return []
        spend = _spend(self._provider, config, period, self._tags)
        if spend.ddb_capped:
            log.warning(
                "CUR carries more than %d dynamodb usage rows — skipping the "
                "capacity-mode check rather than judging on part of it",
                _MAX_DDB_KEYS,
            )
            return []
        if spend.ddb_no_ids and not spend.ddb:
            # without resource ids the rows are a regional lump, and "some table
            # in eu-west-1 is on the wrong mode" is not something you can act on
            log.info(
                "CUR has no resource ids — enable them on the report for the "
                "dynamodb capacity-mode check"
            )
            return []
        return capacity_mode_recommendations(
            spend.ddb,
            _linked_names(self._provider, config, spend),
            self._provider.alias,
            self._tuning,
            spend.currency,
        )


def _linked_names(provider: Provider, config, spend: _Spend) -> dict[str, str]:
    """usage account id -> the alias its spend is reported under.

    Empty unless the payer report is split by account. The payer keeps the alias
    from `clont.yaml`; members get their Organizations name, or the bare id when
    that call isn't available.
    """
    if not config.include_linked:
        return {}
    seen = {account for _, account, *_ in spend.totals if account}
    own = str(getattr(provider, "account_id", None) or "")
    if not seen - {own}:  # single-account report, no need to ask organizations
        return dict.fromkeys(seen, provider.alias)
    org = account_names(provider)
    return {
        account: provider.alias if account == own else org.get(account, account)
        for account in seen
    }


def _spend(provider: Provider, config, period: Period, tags: tuple[str, ...] = ()) -> _Spend:
    folders = _billing_periods(period)
    charges = _Charges.of(config)
    key = "|".join(
        [
            str(provider.alias),
            config.bucket,
            config.prefix,
            config.report_name,
            str(config.include_linked),
            # different charge handling is a different aggregation, not a cache hit
            str(charges),
            *folders,
            *tags,
        ]
    )
    hit = _cache.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < config.refresh_minutes * 60:
        return hit[1]

    s3 = provider.client("s3", config.region)
    only = None if config.include_linked else getattr(provider, "account_id", None)
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
            _read_into(
                s3,
                config.bucket,
                data_key,
                only,
                spend,
                tags,
                split=config.include_linked,
                charges=charges,
            )

    absent = [k for k in tags if k not in spend.seen_tags]
    if absent:
        # the tag exists on the resources but was never activated as a cost
        # allocation tag, so cur has no column for it -> reads as 100% untagged
        log.warning(
            "CUR carries no user tag column for %s — that spend shows as unattributed; "
            "activate the cost allocation tag in Billing",
            ", ".join(absent),
        )

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


def _read_into(
    s3,
    bucket: str,
    key: str,
    only: str | None,
    spend: _Spend,
    tags: tuple[str, ...] = (),
    *,
    split: bool = False,
    charges: _Charges = _Charges(),
) -> None:
    """Stream one gzipped csv part, folding its rows into `spend`.

    `only` keeps just that usage account's rows; `split` groups whatever is left
    by usage account instead of lumping the whole payer report together.
    """
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    with gzip.GzipFile(fileobj=body) as gz:
        reader = csv.DictReader(io.TextIOWrapper(gz, encoding="utf-8"))
        columns = _tag_columns(reader.fieldnames, tags)
        spend.seen_tags.update(k for k, column in columns if column)
        for row in reader:
            owner = _pick(row, _ACCOUNT) if (only is not None or split) else ""
            if only is not None and owner and owner != only:
                continue
            charge = _ADJUSTMENTS.get(_pick(row, _KIND))
            if charge and not charges.keeps(charge):
                continue
            day = _day(row)
            amount = _amount(row, amortize=charges.amortize)
            if day is None:
                continue
            product = _service(row)
            # before the zero-cost skip below: a free-tier dynamodb row carries no
            # money but real traffic, and dropping it would understate the capacity
            # a provisioned table needs
            _ddb_into(row, product, owner if split else "", amount, spend)
            if not amount:
                continue
            spend.add(
                day,
                owner if split else "",
                charge or product,
                # a credit with an ec2 usage type is not data transfer
                "" if charge else transfer_bucket(_pick(row, _USAGE_TYPE), product),
                _row_tags(row, columns),
                amount,
            )
            currency = _pick(row, _CURRENCY)
            if currency and currency != spend.currency:
                spend.currency = currency


def _ddb_into(
    row: dict, service: str, account: str, cost: Decimal, spend: _Spend
) -> None:
    """Keep a dynamodb throughput row's usage amount, keyed by table and bucket."""
    if "dynamodb" not in service.lower().replace(" ", ""):
        return
    if _pick(row, _KIND) != "Usage":
        return  # a credit or refund says nothing about what the table served
    kind = throughput_kind(_pick(row, _USAGE_TYPE))
    if kind is None:
        return  # storage, backups, streams — billed the same in either mode
    table = _pick(row, _RESOURCE)
    if not table:
        spend.ddb_no_ids = True
        return
    start = _start(row)
    if start is None:
        return
    spend.add_ddb(start, account, table, kind, _usage(row), cost)


def _tag_columns(
    fieldnames: list[str] | None, tags: tuple[str, ...]
) -> tuple[tuple[str, str | None], ...]:
    """Each requested tag key paired with its column, None when the report has none.

    Every key stays in the result so a report missing one still says "untagged"
    for it rather than silently dropping the key from the showback.
    """
    if not tags:
        return ()
    present: dict[str, str] = {}
    for column in fieldnames or ():
        for prefix in _TAG_PREFIXES:
            if column.startswith(prefix):
                present[_norm(column[len(prefix) :])] = column
    return tuple((k, present.get(_norm(k))) for k in tags)


def _row_tags(
    row: dict, columns: tuple[tuple[str, str | None], ...]
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (k, (row.get(column) or "").strip() if column else "") for k, column in columns
    )


def _norm(name: str) -> str:
    # data exports lowercase and snake_case the key (CostCenter -> cost_center),
    # legacy cur keeps it verbatim; compare on letters and digits only
    return "".join(c for c in name.lower() if c.isalnum())


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


def _start(row: dict) -> datetime | None:
    """The row's bucket start, hour included — an hourly report shows the shape."""
    stamp = _pick(row, _DAY)
    try:
        return datetime.fromisoformat(stamp[:19].replace("Z", ""))
    except ValueError:
        return None


def _amount(row: dict, *, amortize: bool = True) -> Decimal:
    """What the line costs, with ri/sp commitments spread over their term.

    Unblended puts the whole upfront fee on one day and prices covered usage at
    zero, so the amortized view needs a different column per line type. A report
    that carries none of them stays unblended *everywhere*, zeroing included —
    spreading a fee with nothing to spread it onto drops the charge instead of
    moving it, and unblended at least nets out on its own.
    """
    unblended = _decimal(_pick(row, _COST))
    if not amortize:
        return unblended
    kind = _pick(row, _KIND)
    if kind == "DiscountedUsage":  # ri-covered hour, unblended is 0
        return _amortized(row, (_RI_EFFECTIVE,), unblended)
    if kind == "RIFee":
        # only what nobody used — the used part is already on DiscountedUsage
        return _amortized(row, (_RI_UNUSED_UPFRONT, _RI_UNUSED_RECURRING), unblended)
    if kind == "Fee" and _pick(row, _RI_ARN) and _has(row, _RI_AMORTIZED):
        return Decimal(0)  # all-upfront ri purchase, spread over its RIFee rows
    if kind == "SavingsPlanCoveredUsage":
        return _amortized(row, (_SP_EFFECTIVE,), unblended)
    if kind == "SavingsPlanRecurringFee":
        total = _maybe(row, _SP_TOTAL_COMMITMENT)
        if total is None:
            return unblended
        return total - (_maybe(row, _SP_USED_COMMITMENT) or Decimal(0))
    if kind in ("SavingsPlanNegation", "SavingsPlanUpfrontFee") and _has(row, _SP_AMORTIZED):
        # the negation just cancels the on-demand price of covered usage, and the
        # upfront is amortized through the recurring-fee and covered-usage lines
        return Decimal(0)
    return unblended


def _amortized(
    row: dict, columns: tuple[tuple[str, ...], ...], fallback: Decimal
) -> Decimal:
    """The named amortization columns summed, or `fallback` when the report has none."""
    found = [v for v in (_maybe(row, names) for names in columns) if v is not None]
    return sum(found, Decimal(0)) if found else fallback


def _has(row: dict, columns: tuple[tuple[str, ...], ...]) -> bool:
    """Whether the report carries these columns at all — an empty cell still counts."""
    return any(name in row for names in columns for name in names)


def _maybe(row: dict, names: tuple[str, ...]) -> Decimal | None:
    value = _pick(row, names)
    return _decimal(value) if value else None


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value or "0")
    except InvalidOperation:
        return Decimal(0)


def _usage(row: dict) -> Decimal:
    try:
        return Decimal(_pick(row, _USAGE_AMOUNT) or "0")
    except InvalidOperation:
        return Decimal(0)


def _service(row: dict) -> str:
    # a line with no product (a fee, an adjustment) is labelled by its line-item
    # type, which is exactly how Cost Explorer spells those
    return _pick(row, _SERVICE) or _pick(row, _KIND) or "unknown"
