"""Group 2 health collectors: EBS, Redshift, ASG, ELB, ECS.

These collectors aggregate / make multiple calls, so a small fake client is
clearer than stubbing strict AWS response shapes. Pydantic parse models still
validate the fields we read.
"""

from __future__ import annotations

from clont.monitoring.aws.autoscaling import AutoScalingHealthCollector
from clont.monitoring.aws.ebs import EBSHealthCollector
from clont.monitoring.aws.ecs import ECSHealthCollector
from clont.monitoring.aws.elb import ELBHealthCollector
from clont.monitoring.aws.redshift import RedshiftHealthCollector
from clont.monitoring.models import HealthStatus


class _Paginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **kwargs):
        yield from self._pages


class _FakeClient:
    """Minimal boto3-client stand-in: paginators + ad-hoc method handlers."""

    def __init__(self, paginators: dict | None = None, **methods) -> None:
        self._paginators = paginators or {}
        self._methods = methods

    def get_paginator(self, name: str) -> _Paginator:
        return _Paginator(self._paginators[name])

    def __getattr__(self, name: str):
        handler = self.__dict__["_methods"][name]
        return handler if callable(handler) else (lambda **kw: handler)


class _FakeProvider:
    def __init__(self, clients: dict, alias: str = "prod", region: str = "us-east-1") -> None:
        self._clients = clients
        self.alias = alias
        self._region = region

    def regions(self) -> list[str]:
        return [self._region]

    def client(self, service: str, region: str | None = None):
        return self._clients[service]


def _status_by_id(checks) -> dict:
    return {c.resource.resource_id: c.status for c in checks}


def test_ebs_volume_status():
    ec2 = _FakeClient(
        paginators={
            "describe_volume_status": [
                {
                    "VolumeStatuses": [
                        {"VolumeId": "v-ok", "VolumeStatus": {"Status": "ok"}},
                        {"VolumeId": "v-bad", "VolumeStatus": {"Status": "impaired"}},
                    ]
                }
            ]
        }
    )
    checks = EBSHealthCollector(_FakeProvider({"ec2": ec2})).health()
    assert _status_by_id(checks) == {"v-ok": HealthStatus.OK, "v-bad": HealthStatus.CRITICAL}
    assert all(c.resource.alias == "prod" and c.resource.region == "us-east-1" for c in checks)


def test_redshift_availability():
    rs = _FakeClient(
        paginators={
            "describe_clusters": [
                {
                    "Clusters": [
                        {"ClusterIdentifier": "rs-ok", "ClusterAvailabilityStatus": "Available"},
                        {"ClusterIdentifier": "rs-bad", "ClusterAvailabilityStatus": "Unavailable"},
                        {"ClusterIdentifier": "rs-mod", "ClusterAvailabilityStatus": "Modifying"},
                    ]
                }
            ]
        }
    )
    checks = RedshiftHealthCollector(_FakeProvider({"redshift": rs})).health()
    assert _status_by_id(checks) == {
        "rs-ok": HealthStatus.OK,
        "rs-bad": HealthStatus.CRITICAL,
        "rs-mod": HealthStatus.UNKNOWN,
    }


def test_asg_capacity():
    asg = _FakeClient(
        paginators={
            "describe_auto_scaling_groups": [
                {
                    "AutoScalingGroups": [
                        {
                            "AutoScalingGroupName": "asg-ok",
                            "DesiredCapacity": 1,
                            "Instances": [{"HealthStatus": "Healthy", "LifecycleState": "InService"}],
                        },
                        {
                            "AutoScalingGroupName": "asg-degraded",
                            "DesiredCapacity": 2,
                            "Instances": [
                                {"HealthStatus": "Healthy", "LifecycleState": "InService"},
                                {"HealthStatus": "Unhealthy", "LifecycleState": "InService"},
                            ],
                        },
                        {
                            "AutoScalingGroupName": "asg-down",
                            "DesiredCapacity": 1,
                            "Instances": [{"HealthStatus": "Unhealthy", "LifecycleState": "Pending"}],
                        },
                    ]
                }
            ]
        }
    )
    checks = AutoScalingHealthCollector(_FakeProvider({"autoscaling": asg})).health()
    assert _status_by_id(checks) == {
        "asg-ok": HealthStatus.OK,
        "asg-degraded": HealthStatus.WARN,
        "asg-down": HealthStatus.CRITICAL,
    }


def test_elb_target_health():
    def describe_target_health(TargetGroupArn, **kw):
        if TargetGroupArn == "arn-a":  # one of two unhealthy
            return {
                "TargetHealthDescriptions": [
                    {"TargetHealth": {"State": "healthy"}},
                    {"TargetHealth": {"State": "unhealthy"}},
                ]
            }
        return {"TargetHealthDescriptions": [{"TargetHealth": {"State": "unhealthy"}}]}

    elb = _FakeClient(
        paginators={
            "describe_target_groups": [
                {
                    "TargetGroups": [
                        {"TargetGroupArn": "arn-a", "TargetGroupName": "tg-a"},
                        {"TargetGroupArn": "arn-b", "TargetGroupName": "tg-b"},
                    ]
                }
            ]
        },
        describe_target_health=describe_target_health,
    )
    checks = ELBHealthCollector(_FakeProvider({"elbv2": elb})).health()
    assert _status_by_id(checks) == {"tg-a": HealthStatus.WARN, "tg-b": HealthStatus.CRITICAL}


def test_ecs_services():
    def describe_services(cluster, services, **kw):
        return {
            "services": [
                {"serviceName": "svc-ok", "runningCount": 2, "desiredCount": 2, "deployments": []},
                {"serviceName": "svc-down", "runningCount": 0, "desiredCount": 2, "deployments": []},
                {
                    "serviceName": "svc-failed",
                    "runningCount": 2,
                    "desiredCount": 2,
                    "deployments": [{"rolloutState": "FAILED"}],
                },
            ]
        }

    ecs = _FakeClient(
        paginators={
            "list_clusters": [{"clusterArns": ["cl-1"]}],
            "list_services": [{"serviceArns": ["svc-ok", "svc-down", "svc-failed"]}],
        },
        describe_services=describe_services,
    )
    checks = ECSHealthCollector(_FakeProvider({"ecs": ecs})).health()
    assert _status_by_id(checks) == {
        "svc-ok": HealthStatus.OK,
        "svc-down": HealthStatus.CRITICAL,
        "svc-failed": HealthStatus.CRITICAL,
    }
    assert "deployment failed" in next(c.summary for c in checks if c.resource.resource_id == "svc-failed")
