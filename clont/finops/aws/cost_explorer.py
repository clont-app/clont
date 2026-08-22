"""Account-wide cost via AWS Cost Explorer (ce:GetCostAndUsage).

Cost Explorer is a global endpoint (pinned to us-east-1), so this collector is
account-level and does not iterate regions.

**Billed, so opt-in.** $0.01 per request, once per cycle — ~$86/mo per account at
the default 300s interval, whatever the fleet size. The CUR collector reads the
same numbers out of S3 for free, so this one stays dormant unless the operator
sets `finops.allow_cost_explorer: true` and accepts the charge.
"""

from __future__ import annotations

from datetime import date, timedelta

from clont.core.models import Cloud, Money, Period
from clont.core.registry import register
from clont.finops.base import FinOpsTuning
from clont.finops.models import CostRecord, Recommendation
from clont.providers.aws.parsing import _CEGroup
from clont.providers.base import Provider

_METRIC = "UnblendedCost"


@register("finops", Cloud.AWS, "cost_explorer")
class CostExplorerCollector:
    cloud = Cloud.AWS
    service = "cost_explorer"

    def __init__(self, provider: Provider, tuning: FinOpsTuning | None = None) -> None:
        self._provider = provider
        self._allowed = (tuning or FinOpsTuning()).allow_cost_explorer

    def collect(self, period: Period) -> list[CostRecord]:
        if not self._allowed:
            return []

        ce = self._provider.client("ce", "us-east-1")

        # Our Period is inclusive [start, end]; Cost Explorer's End is
        # exclusive, so push it out one day to include `period.end`.
        time_period = {
            "Start": period.start.isoformat(),
            "End": (period.end + timedelta(days=1)).isoformat(),
        }

        records: list[CostRecord] = []
        next_token: str | None = None
        while True:
            kwargs = {
                "TimePeriod": time_period,
                "Granularity": "DAILY",
                "Metrics": [_METRIC],
                "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
            }
            if next_token:
                kwargs["NextPageToken"] = next_token

            resp = ce.get_cost_and_usage(**kwargs)
            for result in resp.get("ResultsByTime", []):
                # Stamp each record with its own DAILY bucket, not the whole
                # query window — the spend detectors need per-day spend.
                day = date.fromisoformat(result["TimePeriod"]["Start"])
                day_period = Period(start=day, end=day)
                for raw in result.get("Groups", []):
                    records.append(self._to_record(raw, day_period))

            next_token = resp.get("NextPageToken")
            if not next_token:
                break

        return records

    def recommendations(self, period: Period) -> list[Recommendation]:
        # Cost Explorer gives spend, not rightsizing advice — no recommendations here.
        return [] # TODO

    def _to_record(self, raw: dict, period: Period) -> CostRecord:
        group = _CEGroup.model_validate(raw)
        service = group.keys[0] if group.keys else "unknown"
        amount = group.metrics[_METRIC]
        return CostRecord(
            cloud=str(Cloud.AWS),
            service=service,
            period=period,
            alias=self._provider.alias,
            cost=Money(amount=amount.amount, currency=amount.unit),
        )
