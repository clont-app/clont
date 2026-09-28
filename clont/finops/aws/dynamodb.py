"""DynamoDB billing mode: is this table paying on-demand prices for steady traffic?

Compute Optimizer has no DynamoDB surface, so the only free evidence is the bill
itself. CUR carries the usage *amount* per usage type per table, which is exactly
the traffic the table served: `WriteRequestUnits` / `ReadRequestUnits` for an
on-demand table, `WriteCapacityUnit-Hrs` / `ReadCapacityUnit-Hrs` for a
provisioned one.

One provisioned unit covers 3600 requests an hour and costs ~3.5x less than
serving those 3600 on demand, so provisioned wins from roughly **29% sustained
utilization** up. A table serving steady traffic on demand is therefore paying a
premium for elasticity it never uses — that is the finding here.

**Only the on-demand -> provisioned direction.** The other way needs *consumed*
capacity, and CUR only shows what was provisioned, not what was used; that lives
in CloudWatch (`ConsumedRead/WriteCapacityUnits`), which is the billed
`GetMetricData` meter and off by default. So a provisioned table is skipped rather
than guessed at.

What keeps the advice honest:
  * the alternative is modelled **per hour** when the report is hourly, so an
    idle-at-night table pays for its own peaks rather than a flat average;
  * a daily report can't show intra-day peaks at all. rather than inventing a
    peak factor, it has to clear **double** the savings margin, and the summary
    says which granularity it was read at;
  * a floor of one capacity unit per hour, because that is the minimum you can
    provision;
  * the on-demand side is the **billed** figure from CUR, not a modelled one;
  * tables using the IA table class, global-table replicated writes or vector
    writes are skipped — those usage types don't map onto the four plain skus.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from clont.core.logging import get_logger
from clont.core.models import Cloud, CloudResource, Money
from clont.finops.aws.pricing import (
    DDB_UNIT_REQUESTS_PER_HOUR,
    HOURS_PER_MONTH,
    dynamodb_provisioned_quote,
)
from clont.finops.base import FinOpsTuning
from clont.finops.models import Recommendation

log = get_logger("clont.finops.aws.dynamodb")

KIND = "capacity-mode"
# usage types we can model, by what cur calls them (region prefix stripped)
_KINDS = {
    "WriteCapacityUnit-Hrs": "wcu_hours",
    "ReadCapacityUnit-Hrs": "rcu_hours",
    "WriteRequestUnits": "write_requests",
    "ReadRequestUnits": "read_requests",
}
_CAPACITY = ("wcu_hours", "rcu_hours")
_REQUESTS = ("write_requests", "read_requests")
# throughput we can't map onto those four: IA table class, global tables, vectors
_OTHER = "other"
# region code prefix cur puts on a usage type outside us-east-1 (USW2-, EUC1-, EU-)
_PREFIX = re.compile(r"^[A-Z]{2,5}[0-9]?-")
# what marks throughput we don't model. checked *before* the prefix is stripped,
# because `IA-` is shaped exactly like a region code: strip it and the IA table
# class reads as the plain sku at a third of the price
_NOT_PLAIN = re.compile(r"(?:^|-)IA-|Repl|Vector")
_ARN = re.compile(r"^arn:[^:]*:dynamodb:([a-z0-9-]+):[0-9]*:table/(.+)$")


def throughput_kind(usage_type: str) -> str | None:
    """Our name for a dynamodb throughput usage type.

    `None` for anything that isn't throughput (storage, backup, streams — those
    are billed the same whichever mode the table is in). `"other"` for throughput
    we deliberately don't model, which disqualifies the table.
    """
    bare = _PREFIX.sub("", usage_type, count=1)
    if not _NOT_PLAIN.search(usage_type):
        kind = _KINDS.get(bare)
        if kind is not None:
            return kind
    if "CapacityUnit" in bare or "RequestUnit" in bare or "Request" in bare:
        return _OTHER
    return None


@dataclass
class _Table:
    """One table's throughput usage, bucketed by the report's own granularity."""

    arn: str
    alias: str | None
    buckets: dict[datetime, dict[str, Decimal]] = field(default_factory=dict)
    cost: Decimal = Decimal(0)
    modellable: bool = True

    def add(self, start: datetime, kind: str, usage: Decimal, cost: Decimal) -> None:
        if kind == _OTHER:
            self.modellable = False
            return
        self.buckets.setdefault(start, {})
        self.buckets[start][kind] = self.buckets[start].get(kind, Decimal(0)) + usage
        self.cost += cost

    @property
    def region(self) -> str | None:
        m = _ARN.match(self.arn)
        return m.group(1) if m else None

    @property
    def name(self) -> str:
        m = _ARN.match(self.arn)
        return m.group(2) if m else self.arn


def capacity_mode_recommendations(
    usage: dict[tuple[datetime, str, str, str], tuple[Decimal, Decimal]],
    aliases: dict[str, str],
    alias: str | None,
    tuning: FinOpsTuning | None = None,
    currency: str = "USD",
) -> list[Recommendation]:
    """On-demand tables whose own traffic says provisioned would be cheaper.

    `usage` is keyed `(bucket start, usage account, table arn, kind)` -> `(usage
    amount, cost)` — what `cur.py` collects while it streams the report.
    """
    tuning = tuning or FinOpsTuning()
    if currency != "USD":
        # the price table is USD only; a EUR bill would compare two currencies
        log.info("CUR is in %s — skipping the dynamodb capacity-mode check", currency)
        return []

    tables: dict[tuple[str, str], _Table] = {}
    for (start, account, arn, kind), (amount, cost) in usage.items():
        table = tables.setdefault(
            (account, arn), _Table(arn=arn, alias=aliases.get(account, alias))
        )
        table.add(start, kind, amount, cost)

    out = []
    for table in tables.values():
        rec = _advise(table, tuning)
        if rec is not None:
            out.append(rec)
    return out


def _advise(table: _Table, tuning: FinOpsTuning) -> Recommendation | None:
    if not table.modellable or not table.buckets:
        return None
    kinds = {k for bucket in table.buckets.values() for k in bucket}
    if kinds & set(_CAPACITY):
        return None  # provisioned already; cur can't show what it consumed
    if not kinds & set(_REQUESTS) or table.cost <= 0:
        return None  # no traffic, or all of it inside the free tier

    starts = sorted(table.buckets)
    hourly = len({s.hour for s in starts}) > 1
    bucket_hours = Decimal(1 if hourly else 24)
    if len(starts) * bucket_hours < Decimal(str(tuning.ddb_min_hours)):
        return None  # too short a window to call the traffic steady

    target = Decimal(str(tuning.ddb_target_utilization))
    region = table.region
    per_bucket = DDB_UNIT_REQUESTS_PER_HOUR * bucket_hours
    modelled = Decimal(0)
    reads: list[int] = []
    writes: list[int] = []
    approximate = True
    priced_region = region
    for start in starts:
        bucket = table.buckets[start]
        rcu = _units(bucket.get("read_requests", Decimal(0)), per_bucket, target)
        wcu = _units(bucket.get("write_requests", Decimal(0)), per_bucket, target)
        quote = dynamodb_provisioned_quote(
            Decimal(rcu), Decimal(wcu), bucket_hours, region
        )
        modelled += quote.amount
        approximate = quote.approximate
        priced_region = quote.region
        reads.append(rcu)
        writes.append(wcu)

    saving = table.cost - modelled
    if saving <= 0:
        return None
    pct = saving / table.cost * 100
    hours = Decimal(len(starts)) * bucket_hours
    monthly = saving / hours * HOURS_PER_MONTH
    # a daily report can only model the average, so it has to clear double the
    # margin — enough to still be a saving if the real shape is peaky
    min_pct = Decimal(str(tuning.ddb_min_savings_pct)) * (1 if hourly else 2)
    if pct < min_pct:
        return None
    if monthly < Decimal(str(tuning.ddb_min_savings_usd)):
        return None

    grain = (
        "modelled hour by hour"
        if hourly
        else "daily CUR granularity — a peaky day needs more capacity than this"
    )
    return Recommendation(
        cloud=str(Cloud.AWS),
        service="dynamodb",
        kind=KIND,
        resource=CloudResource(
            cloud=Cloud.AWS,
            service="dynamodb",
            resource_id=table.name,
            region=region,
            alias=table.alias,
        ),
        summary=(
            f"On-demand billing costs {table.cost:.2f} USD per {hours:.0f}h of traffic; "
            f"provisioned at ~{_typical(writes)} WCU / ~{_typical(reads)} RCU with "
            f"autoscaling models at {modelled:.2f} ({pct:.0f}% less) — switch if the "
            f"traffic stays this steady ({grain})"
        ),
        estimated_savings=Money(amount=monthly.quantize(Decimal("0.01")), currency="USD"),
        priced_region=priced_region,
        approximate=approximate,
    )


def _units(requests: Decimal, per_bucket: Decimal, target: Decimal) -> int:
    """Capacity units needed to serve `requests` in one bucket at `target` load.

    At least 1: one unit per hour is the floor you can provision, so an idle hour
    is not free under provisioned billing.
    """
    needed = requests / per_bucket / target
    return max(1, math.ceil(needed))


def _typical(units: list[int]) -> int:
    """The median bucket's capacity — what the table looks like most of the time."""
    ordered = sorted(units)
    return ordered[len(ordered) // 2]
