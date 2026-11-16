"""finops idle detection for dynamodb: a table nobody reads or writes.

consumed read/write capacity is the signal, and both billing modes publish it.
dynamodb sends no datapoint for a quiet period, so here "no data" means zero —
which is only safe once the table is older than the window, a table created
yesterday has no history either. reads through a gsi are metered on the index,
so every gsi is queried too, else a table read only via its index looks idle.

provisioned tables pay for capacity whether used or not, on-demand ones only
for storage, so the advice differs.

off unless `finops.allow_cloudwatch_metrics` is set, same trade-off as `idle.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.base import FinOpsTuning
from clont.finops.models import CostRecord, Recommendation
from clont.providers.aws.metrics import (
    metric_query_dims,
    metric_values,
    run_metric_queries,
)
from clont.providers.aws.regions import for_each_region
from clont.providers.base import Provider

_PERIOD = 86400
_KIND = "idle"
_READ = "ConsumedReadCapacityUnits"
_WRITE = "ConsumedWriteCapacityUnits"


@register("finops", Cloud.AWS, "idle_dynamodb")
class IdleDynamoDBCollector:
    cloud = Cloud.AWS
    service = "idle_dynamodb"
    recommend_every_seconds = 86400

    def __init__(self, provider: Provider, tuning: FinOpsTuning | None = None) -> None:
        self._provider = provider
        self._tuning = tuning or FinOpsTuning()

    def collect(self, period: Period) -> list[CostRecord]:
        return []

    def recommendations(self, period: Period) -> list[Recommendation]:
        if not self._tuning.allow_cloudwatch_metrics:
            return []
        return for_each_region(self._provider, self._region, what="idle dynamodb")

    def _region(self, region: str) -> list[Recommendation]:
        ddb = self._provider.client("dynamodb", region)
        end = datetime.now(UTC)
        start = end - timedelta(days=self._tuning.idle_lookback_days)
        tables = self._old_tables(ddb, start)
        if not tables:
            return []
        used = self._used(region, tables, start, end)
        return [
            self._rec(name, region, mode)
            for name, (mode, _) in tables.items()
            if not used.get(name)
        ]

    def _old_tables(self, ddb, start: datetime) -> dict[str, tuple[str, list[str]]]:
        """{table: (billing mode, [gsi names])} for active tables older than the window"""
        tables: dict[str, tuple[str, list[str]]] = {}
        for page in ddb.get_paginator("list_tables").paginate():
            for name in page.get("TableNames", []):
                t = ddb.describe_table(TableName=name).get("Table", {})
                created = t.get("CreationDateTime")
                if t.get("TableStatus") != "ACTIVE" or created is None or created > start:
                    continue
                # tables made before on-demand existed carry no summary: provisioned
                mode = t.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED")
                gsis = [g["IndexName"] for g in t.get("GlobalSecondaryIndexes", [])]
                tables[name] = (mode, gsis)
        return tables

    def _used(self, region: str, tables: dict, start: datetime, end: datetime) -> dict[str, bool]:
        queries: list[dict] = []
        id_map: dict[str, str] = {}

        def add(table: str, metric: str, dims: dict[str, str]) -> None:
            qid = f"q{len(queries)}"
            id_map[qid] = table
            queries.append(
                metric_query_dims(qid, "AWS/DynamoDB", metric, dims, _PERIOD, stat="Sum")
            )

        for name, (_, gsis) in tables.items():
            add(name, _READ, {"TableName": name})
            add(name, _WRITE, {"TableName": name})
            for gsi in gsis:
                add(name, _READ, {"TableName": name, "GlobalSecondaryIndexName": gsi})

        cw = self._provider.client("cloudwatch", region)
        series = run_metric_queries(cw, queries, start, end)
        used: dict[str, bool] = {name: False for name in tables}
        for qid, name in id_map.items():
            if sum(metric_values(series, qid)) > 0:
                used[name] = True
        return used

    def _rec(self, table: str, region: str, mode: str) -> Recommendation:
        days = self._tuning.idle_lookback_days
        if mode == "PAY_PER_REQUEST":
            advice = "on-demand, only storage is billed — consider removing"
        else:
            advice = "provisioned capacity paid for nothing — consider removing or on-demand"
        return Recommendation(
            cloud=str(Cloud.AWS),
            service="dynamodb",
            kind=_KIND,
            resource=CloudResource(
                cloud=Cloud.AWS,
                service="dynamodb",
                resource_id=table,
                region=region,
                alias=self._provider.alias,
            ),
            summary=f"Idle {days}d (no reads or writes, {advice})",
            estimated_savings=Money(amount=Decimal(0)),  # unknown: not priced here
        )
