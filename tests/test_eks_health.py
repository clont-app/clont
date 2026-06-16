"""EKS health collector: list+describe, status/issue mapping, via Stubber."""

from __future__ import annotations

import boto3
from botocore.stub import Stubber

from clont.events.detectors import HealthDetector
from clont.monitoring.aws.eks import EKSHealthCollector
from clont.monitoring.models import HealthStatus


class _FakeProvider:
    def __init__(self, client, alias: str = "prod") -> None:
        self._client = client
        self.alias = alias

    def regions(self) -> list[str]:
        return ["us-east-1"]

    def client(self, service: str, region: str | None = None):
        assert service == "eks"
        return self._client


def _cluster(name: str, status: str, issues: list[dict] | None = None) -> dict:
    return {"cluster": {"name": name, "status": status, "health": {"issues": issues or []}}}


def test_eks_health_maps_status_issues_and_attributes():
    client = boto3.client(
        "eks", region_name="us-east-1",
        aws_access_key_id="x", aws_secret_access_key="x",
    )
    with Stubber(client) as stubber:
        stubber.add_response("list_clusters", {"clusters": ["c-ok", "c-issue", "c-failed"]})
        stubber.add_response("describe_cluster", _cluster("c-ok", "ACTIVE"))
        stubber.add_response(
            "describe_cluster",
            _cluster("c-issue", "ACTIVE", [{"code": "InsufficientNumberOfReplicas", "message": "x"}]),
        )
        stubber.add_response("describe_cluster", _cluster("c-failed", "FAILED"))

        checks = EKSHealthCollector(_FakeProvider(client)).health()

    by_id = {c.resource.resource_id: c for c in checks}
    assert by_id["c-ok"].status is HealthStatus.OK
    assert by_id["c-issue"].status is HealthStatus.CRITICAL
    assert "InsufficientNumberOfReplicas" in by_id["c-issue"].summary
    assert by_id["c-failed"].status is HealthStatus.CRITICAL
    assert all(c.resource.region == "us-east-1" and c.resource.alias == "prod" for c in checks)

    events = HealthDetector().detect(checks)
    assert {e.key for e in events} == {
        "monitoring:health:prod:aws:eks:c-issue",
        "monitoring:health:prod:aws:eks:c-failed",
    }
