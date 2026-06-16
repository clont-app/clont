"""EC2 metrics collector (CloudWatch GetMetricData), via botocore Stubber."""

from __future__ import annotations

from datetime import UTC, datetime

import boto3
import pytest
from botocore.stub import Stubber

from clont.core.models import Period
from clont.monitoring.aws.ec2 import EC2MetricCollector

PERIOD = Period(start=datetime(2024, 1, 1).date(), end=datetime(2024, 1, 1).date())
_T1 = datetime(2024, 1, 1, 10, tzinfo=UTC)
_T2 = datetime(2024, 1, 1, 11, tzinfo=UTC)


class _FakeProvider:
    def __init__(self, ec2, cw, alias: str = "prod", regions=("us-east-1",)) -> None:
        self._ec2 = ec2
        self._cw = cw
        self.alias = alias
        self._regions = list(regions)

    def regions(self) -> list[str]:
        return self._regions

    def client(self, service: str, region: str | None = None):
        return {"ec2": self._ec2, "cloudwatch": self._cw}[service]


def _client(service: str):
    return boto3.client(
        service,
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


def test_collect_maps_metric_points():
    ec2, cw = _client("ec2"), _client("cloudwatch")
    ec2_s, cw_s = Stubber(ec2), Stubber(cw)
    ec2_s.add_response("describe_instance_status", {"InstanceStatuses": [{"InstanceId": "i-123"}]})
    # queries are emitted as q0=CPU, q1=NetworkIn, q2=NetworkOut for the one instance.
    cw_s.add_response(
        "get_metric_data",
        {
            "MetricDataResults": [
                {"Id": "q0", "Timestamps": [_T2, _T1], "Values": [15.0, 12.0], "StatusCode": "Complete"},
                {"Id": "q1", "Timestamps": [_T1], "Values": [1000.0], "StatusCode": "Complete"},
                {"Id": "q2", "Timestamps": [_T1], "Values": [2000.0], "StatusCode": "Complete"},
            ]
        },
    )
    ec2_s.activate()
    cw_s.activate()

    points = EC2MetricCollector(_FakeProvider(ec2, cw)).collect(PERIOD)

    # 2 CPU points + 1 NetworkIn + 1 NetworkOut = 4.
    assert len(points) == 4
    cpu = [p for p in points if p.name == "CPUUtilization"]
    assert sorted(p.value for p in cpu) == [12.0, 15.0]
    assert all(p.unit == "Percent" for p in cpu)

    netin = next(p for p in points if p.name == "NetworkIn")
    assert (netin.value, netin.unit) == (1000.0, "Bytes")

    for p in points:
        assert p.resource.resource_id == "i-123"
        assert p.resource.region == "us-east-1"
        assert p.resource.alias == "prod"

    ec2_s.deactivate()
    cw_s.deactivate()


def test_collect_no_instances_skips_cloudwatch():
    ec2, cw = _client("ec2"), _client("cloudwatch")
    ec2_s = Stubber(ec2)
    ec2_s.add_response("describe_instance_status", {"InstanceStatuses": []})
    ec2_s.activate()
    # cw has no queued responses; if collect() called it, Stubber would error.
    cw_s = Stubber(cw)
    cw_s.activate()

    points = EC2MetricCollector(_FakeProvider(ec2, cw)).collect(PERIOD)

    assert points == []
    ec2_s.deactivate()
    cw_s.deactivate()
