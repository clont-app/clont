"""RDS health collector: status mapping + attribution, via Stubber."""

from __future__ import annotations

import boto3
from botocore.stub import Stubber

from clont.events.detectors import HealthDetector
from clont.monitoring.aws.rds import RDSHealthCollector
from clont.monitoring.models import HealthStatus


class _FakeProvider:
    def __init__(self, client, alias: str = "prod") -> None:
        self._client = client
        self.alias = alias

    def regions(self) -> list[str]:
        return ["us-east-1"]

    def client(self, service: str, region: str | None = None):
        assert service == "rds"
        return self._client


def test_rds_health_maps_status_and_attributes():
    client = boto3.client(
        "rds", region_name="us-east-1",
        aws_access_key_id="x", aws_secret_access_key="x",
    )
    with Stubber(client) as stubber:
        stubber.add_response(
            "describe_db_instances",
            {
                "DBInstances": [
                    {"DBInstanceIdentifier": "db-ok", "DBInstanceStatus": "available"},
                    {"DBInstanceIdentifier": "db-full", "DBInstanceStatus": "storage-full"},
                    {"DBInstanceIdentifier": "db-new", "DBInstanceStatus": "creating"},
                ]
            },
        )
        checks = RDSHealthCollector(_FakeProvider(client)).health()

    by_id = {c.resource.resource_id: c for c in checks}
    assert by_id["db-ok"].status is HealthStatus.OK
    assert by_id["db-full"].status is HealthStatus.CRITICAL
    assert by_id["db-new"].status is HealthStatus.UNKNOWN
    for c in checks:
        assert c.resource.region == "us-east-1"
        assert c.resource.alias == "prod"

    events = HealthDetector().detect(checks)
    assert {e.key for e in events} == {"monitoring:health:prod:aws:rds:db-full"}
