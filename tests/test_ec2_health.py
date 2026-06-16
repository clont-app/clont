"""EC2 health collector: status mapping, region traversal, isolation.

Deterministic cases use botocore Stubber (we need to inject impaired statuses,
which moto won't); one moto test proves the real paginator + region path.
"""

from __future__ import annotations

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from clont.events.detectors import HealthDetector
from clont.monitoring.aws.ec2 import EC2MetricCollector, _overall
from clont.monitoring.models import HealthStatus


def _inst(instance_id: str, system: str = "ok", instance: str = "ok") -> dict:
    return {
        "InstanceId": instance_id,
        "AvailabilityZone": "us-east-1a",
        "InstanceState": {"Code": 16, "Name": "running"},
        "SystemStatus": {"Status": system, "Details": []},
        "InstanceStatus": {"Status": instance, "Details": []},
    }


class _FakeProvider:
    def __init__(self, clients: dict[str, object], alias: str = "prod") -> None:
        self._clients = clients  # region -> ec2 client
        self.alias = alias

    def regions(self) -> list[str]:
        return list(self._clients)

    def client(self, service: str, region: str | None = None):
        assert service == "ec2"
        return self._clients[region]


def _ec2_client():
    return boto3.client(
        "ec2",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


# --- pure mapping ----------------------------------------------------------


def test_overall_picks_worse_check():
    assert _overall("ok", "ok")[0] is HealthStatus.OK
    assert _overall("ok", "impaired")[0] is HealthStatus.CRITICAL
    assert _overall("insufficient-data", "ok")[0] is HealthStatus.WARN
    assert _overall("ok", "initializing")[0] is HealthStatus.UNKNOWN
    assert "system: ok, instance: impaired" == _overall("ok", "impaired")[1]


# --- mapping + pagination + attribution ------------------------------------


def test_health_maps_statuses_and_attributes():
    client = _ec2_client()
    with Stubber(client) as stubber:
        stubber.add_response(
            "describe_instance_status",
            {"InstanceStatuses": [_inst("i-ok")], "NextToken": "p2"},
            {},
        )
        stubber.add_response(
            "describe_instance_status",
            {
                "InstanceStatuses": [
                    _inst("i-bad", system="ok", instance="impaired"),
                    _inst("i-warn", system="insufficient-data", instance="ok"),
                ]
            },
            {"NextToken": "p2"},
        )

        checks = EC2MetricCollector(_FakeProvider({"us-east-1": client})).health()

    by_id = {c.resource.resource_id: c for c in checks}
    assert by_id["i-ok"].status is HealthStatus.OK
    assert by_id["i-bad"].status is HealthStatus.CRITICAL
    assert by_id["i-warn"].status is HealthStatus.WARN
    for c in checks:
        assert c.resource.alias == "prod"
        assert c.resource.region == "us-east-1"

    # Only the non-OK checks become events, with alias folded into the key.
    events = HealthDetector().detect(checks)
    assert {e.key for e in events} == {
        "monitoring:health:prod:aws:ec2:i-bad",
        "monitoring:health:prod:aws:ec2:i-warn",
    }


# --- region traversal ------------------------------------------------------


def test_health_traverses_all_regions():
    clients = {}
    stubbers = []
    for region, iid in (("us-east-1", "i-east"), ("eu-west-1", "i-west")):
        c = _ec2_client()
        s = Stubber(c)
        s.add_response("describe_instance_status", {"InstanceStatuses": [_inst(iid)]}, {})
        s.activate()
        stubbers.append(s)
        clients[region] = c

    checks = EC2MetricCollector(_FakeProvider(clients)).health()

    region_of = {c.resource.resource_id: c.resource.region for c in checks}
    assert region_of == {"i-east": "us-east-1", "i-west": "eu-west-1"}
    for s in stubbers:
        s.deactivate()


# --- per-region isolation --------------------------------------------------


def test_one_bad_region_is_skipped():
    good = _ec2_client()
    bad = _ec2_client()
    good_s, bad_s = Stubber(good), Stubber(bad)
    good_s.add_response("describe_instance_status", {"InstanceStatuses": [_inst("i-good")]}, {})
    bad_s.add_client_error("describe_instance_status", service_error_code="UnauthorizedOperation")
    good_s.activate()
    bad_s.activate()

    checks = EC2MetricCollector(
        _FakeProvider({"eu-west-1": good, "us-east-1": bad})
    ).health()

    assert [c.resource.resource_id for c in checks] == ["i-good"]
    good_s.deactivate()
    bad_s.deactivate()


# --- moto happy path -------------------------------------------------------


@pytest.fixture
def aws_creds(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@mock_aws
def test_health_against_moto(aws_creds):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1)

    checks = EC2MetricCollector(_FakeProvider({"us-east-1": ec2})).health()

    assert len(checks) >= 1
    assert checks[0].resource.region == "us-east-1"
    assert checks[0].resource.alias == "prod"
