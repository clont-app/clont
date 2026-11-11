"""Idle agent stack: a floor that kept billing while the agent saw no tokens.

The one finding a token tracker cannot produce, because it never sees the
floor. An agent (named by `finops.agent_tag_keys`) whose `floor` rows ran
through the last `agent_idle_days` days with no token row in that window is
paying for a collection / endpoint / provisioned throughput nobody calls.

No prices. The saving is the agent's own floor over the window, restated as a
monthly run-rate, so it is the bill and not an estimate. Unattributed spend is
never judged: idle "nobody" is not something you can switch off.

An agent tagged non-prod (`finops.nonprod_tags`) gets the off-hours advice
instead, under the same kind `offhours.py` uses, since dev stacks are expected
to sit quiet.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.agentspend import DIMENSION, FLOOR, TOKENS, UNATTRIBUTED
from clont.finops.aws.cur import CURCostCollector
from clont.finops.aws.offhours import _KIND as OFFHOURS_KIND
from clont.finops.base import FinOpsTuning
from clont.finops.models import CostRecord, Recommendation
from clont.providers.base import Provider

KIND = "idle-agent"
SERVICE = "agent"

_MONTH_DAYS = Decimal(30)
_CENT = Decimal("0.01")
# hours outside a 12h weekday window, the share an off-hours schedule stops
_OFF_HOURS_SHARE = Decimal(168 - 5 * 12) / Decimal(168)
# cur lags a day or two, read a little past the window so it still fills
_LAG_DAYS = 7


def idle_agent_recommendations(
    records: list[CostRecord],
    tag_keys: tuple[str, ...],
    idle_days: int,
    nonprod_tags: dict[str, tuple[str, ...]] | None = None,
) -> list[Recommendation]:
    """One recommendation per agent whose floor ran the whole window with no tokens."""
    if not tag_keys or idle_days <= 0 or not records:
        return []
    # the window ends on the last day the bill has, not today: cur lags
    first = min(r.period.start for r in records)
    last = max(r.period.end for r in records)
    start = last - timedelta(days=idle_days - 1)
    if first > start:
        return []  # not enough history to call anything idle

    nonprod = {k: {v.lower() for v in vs} for k, vs in (nonprod_tags or {}).items()}
    floor: dict[tuple[str, str | None, str], Decimal] = defaultdict(Decimal)
    floor_days: dict[tuple[str, str | None, str], set[date]] = defaultdict(set)
    busy: set[tuple[str, str | None, str]] = set()
    labels: dict[tuple[str, str | None, str], str] = {}
    for record in records:
        bucket = ((record.dimensions or {}).get(DIMENSION) or "").strip()
        if not bucket or record.period.end < start:
            continue
        agent = _agent(record, tag_keys)
        if agent == UNATTRIBUTED:
            continue
        key = (agent, record.alias, record.cost.currency)
        if bucket in TOKENS:
            busy.add(key)
        elif bucket == FLOOR:
            floor[key] += record.cost.amount
            floor_days[key].add(record.period.start)
            label = _nonprod_label(record.tags or {}, nonprod)
            if label and key not in labels:
                labels[key] = label

    out: list[Recommendation] = []
    for key in sorted(floor, key=lambda k: (str(k[1] or ""), k[2], k[0])):
        # kept running = billed on the first and the last day of the window
        if key in busy or floor[key] <= 0 or not {start, last} <= floor_days[key]:
            continue
        run_rate = _money(floor[key] / Decimal(idle_days) * _MONTH_DAYS)
        out.append(_rec(key, run_rate, idle_days, labels.get(key)))
    return out


def _rec(
    key: tuple[str, str | None, str], run_rate: Decimal, idle_days: int, label: str | None
) -> Recommendation:
    agent, alias, currency = key
    resource = CloudResource(cloud=Cloud.AWS, service=SERVICE, resource_id=agent, alias=alias)
    if label:
        saving = _money(run_rate * _OFF_HOURS_SHARE)
        return Recommendation(
            cloud=str(Cloud.AWS),
            service=SERVICE,
            kind=OFFHOURS_KIND,
            resource=resource,
            summary=(
                f"Non-prod ({label}) agent '{agent}' floor billed {run_rate} {currency}/mo "
                f"with no token traffic in {idle_days}d — schedule it off nights + "
                "weekends, or tear it down between uses"
            ),
            estimated_savings=Money(amount=saving, currency=currency),
            approximate=True,  # the share of hours is a schedule assumption
        )
    return Recommendation(
        cloud=str(Cloud.AWS),
        service=SERVICE,
        kind=KIND,
        resource=resource,
        summary=(
            f"Agent '{agent}' floor billed {run_rate} {currency}/mo with no token "
            f"traffic in {idle_days}d — delete or shrink its collection / endpoint / "
            "provisioned throughput"
        ),
        estimated_savings=Money(amount=run_rate, currency=currency),
        approximate=False,  # its own cur run-rate, not a price list
    )


def _agent(record: CostRecord, keys: tuple[str, ...]) -> str:
    tags = record.tags or {}
    for key in keys:
        value = (tags.get(key) or "").strip()
        if value:
            return value
    return UNATTRIBUTED


def _nonprod_label(tags: dict[str, str], nonprod: dict[str, set[str]]) -> str | None:
    for key, values in nonprod.items():
        value = tags.get(key)
        if value is not None and value.lower() in values:
            return f"{key}={value}"
    return None


def _money(amount: Decimal) -> Decimal:
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


@register("finops", Cloud.AWS, "idle_agent")
class IdleAgentCollector:
    cloud = Cloud.AWS
    service = "idle_agent"
    # a daily bill, a daily judgement
    recommend_every_seconds = 86400

    def __init__(self, provider: Provider, tuning: FinOpsTuning | None = None) -> None:
        self._provider = provider
        self._tuning = tuning or FinOpsTuning()

    def collect(self, period: Period) -> list[CostRecord]:
        return []  # the cur collector already reports this spend

    def recommendations(self, period: Period) -> list[Recommendation]:
        tuning = self._tuning
        if not tuning.agent_tag_keys or getattr(self._provider, "cur", None) is None:
            return []
        # nonprod keys must ride the cur tag split or no record carries them,
        # already-required keys keep the cache key so usually no second read
        tags = tuple(dict.fromkeys((*tuning.required_tags, *tuning.nonprod_tags)))
        cur = CURCostCollector(self._provider, replace(tuning, required_tags=tags))
        end = date.today() - timedelta(days=1)
        start = end - timedelta(days=tuning.agent_idle_days + _LAG_DAYS)
        records = cur.collect(Period(start=start, end=end))
        return idle_agent_recommendations(
            records, tuning.agent_tag_keys, tuning.agent_idle_days, tuning.nonprod_tags
        )
