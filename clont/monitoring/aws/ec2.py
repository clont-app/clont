"""EC2 monitoring: instance reachability health + CloudWatch metrics.

 *regional* collector: `health()` walks every region in scope for the
account and reads `describe_instance_status`, mapping the system/instance
reachability checks to a `HealthCheck` per running instance; `collect()` pulls
CPU/network time series from CloudWatch for the same instances. A region that
denies access or is disabled is skipped, not fatal
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from clont.core.models import Cloud, CloudResource, Period
from clont.core.registry import register
from clont.monitoring.aws._common import (
    MetricsPolicy,
    for_each_region,
    metric_query,
    run_metric_queries,
)
from clont.monitoring.models import HealthCheck, HealthStatus, MetricPoint
from clont.providers.aws.parsing import _EC2Status
from clont.providers.base import Provider

# CloudWatch EC2 metrics to collect, with their unit. Averaged hourly.
# CPUCreditBalance is only published by burstable (T-family) instances; others
# return no datapoints and are skipped naturally, so it's safe to always request.
_METRICS = (
    ("CPUUtilization", "Percent"),
    ("NetworkIn", "Bytes"),
    ("NetworkOut", "Bytes"),
    ("CPUCreditBalance", "Count"),
)
_PERIOD_SECONDS = 3600

# AWS status-check value
_HEALTH = {
    "ok": HealthStatus.OK,
    "not-applicable": HealthStatus.OK,
    "impaired": HealthStatus.CRITICAL,
    "insufficient-data": HealthStatus.WARN,
    "initializing": HealthStatus.UNKNOWN,
}

# Severity order for picking the worse
_RANK = {
    HealthStatus.OK: 0,
    HealthStatus.UNKNOWN: 1,
    HealthStatus.WARN: 2,
    HealthStatus.CRITICAL: 3,
}


def _overall(system: str, instance: str) -> tuple[HealthStatus, str]:
    """Combine the system + instance checks into one status + summary."""
    sys_status = _HEALTH.get(system, HealthStatus.UNKNOWN)
    inst_status = _HEALTH.get(instance, HealthStatus.UNKNOWN)
    worst = max(sys_status, inst_status, key=_RANK.__getitem__)
    return worst, f"system: {system}, instance: {instance}"


@register("monitoring", Cloud.AWS, "ec2")
class EC2MetricCollector:
    cloud = Cloud.AWS
    service = "ec2"

    def __init__(self, provider: Provider, metrics: MetricsPolicy | None = None) -> None:
        self._provider = provider
        self._metrics = metrics

    def collect(self, period: Period) -> list[MetricPoint]:
        """CloudWatch CPU/network time series per running instance, per region."""
        # Period is an inclusive [start, end] of dates; CloudWatch wants
        # datetimes with an exclusive end, so push end out one day.
        start = datetime.combine(period.start, time.min, tzinfo=UTC)
        end = datetime.combine(period.end + timedelta(days=1), time.min, tzinfo=UTC)

        return for_each_region(
            self._provider,
            lambda region: self._region_metrics(region, start, end),
            what="ec2 metrics",
        )

    def _region_metrics(
        self, region: str, start: datetime, end: datetime
    ) -> list[MetricPoint]:
        ec2 = self._provider.client("ec2", region)
        instance_ids = self._running_instance_ids(ec2)
        if not instance_ids:
            return []

        # Build one MetricStat query per (instance, metric); map each query id
        # back to what it measures so results can be attributed.
        queries: list[dict] = []
        id_map: dict[str, tuple[str, str, str]] = {}
        for n, (iid, (name, unit)) in enumerate(
            (iid, m) for iid in instance_ids for m in _METRICS
        ):
            qid = f"q{n}"
            id_map[qid] = (iid, name, unit)
            queries.append(
                metric_query(
                    qid, "AWS/EC2", name, "InstanceId", iid, _PERIOD_SECONDS
                )
            )

        cw = self._provider.client("cloudwatch", region)
        series = run_metric_queries(cw, queries, start, end, self._metrics)
        points: list[MetricPoint] = []
        for qid, pairs in series.items():
            iid, name, unit = id_map[qid]
            resource = CloudResource(
                cloud=Cloud.AWS,
                service="ec2",
                resource_id=iid,
                region=region,
                alias=self._provider.alias,
            )
            points.extend(
                MetricPoint(
                    name=name, value=value, unit=unit, timestamp=ts, resource=resource
                )
                for ts, value in pairs
            )
        return points

    def _running_instance_ids(self, ec2) -> list[str]:
        ids: list[str] = []
        for page in ec2.get_paginator("describe_instance_status").paginate():
            ids.extend(s["InstanceId"] for s in page.get("InstanceStatuses", []))
        return ids

    def health(self) -> list[HealthCheck]:
        return for_each_region(self._provider, self._region_health, what="ec2 health")

    def _region_health(self, region: str) -> list[HealthCheck]:
        ec2 = self._provider.client("ec2", region)
        checks: list[HealthCheck] = []
        for page in ec2.get_paginator("describe_instance_status").paginate():
            for raw in page.get("InstanceStatuses", []):
                st = _EC2Status.model_validate(raw)
                status, summary = _overall(st.system_status, st.instance_status)
                checks.append(
                    HealthCheck(
                        resource=CloudResource(
                            cloud=Cloud.AWS,
                            service="ec2",
                            resource_id=st.instance_id,
                            region=region,
                            alias=self._provider.alias,
                        ),
                        status=status,
                        summary=summary,
                    )
                )
        return checks
