"""Idle recommendations from Compute Optimizer — the free replacement for the
CloudWatch idle detectors.

`GetIdleRecommendations` covers all six resource types the metric detectors did,
and unlike them it carries a real savings figure.
"""

from __future__ import annotations

from decimal import Decimal

from botocore.exceptions import ClientError

from clont.events.detectors import RecommendationDetector
from clont.finops.aws.compute_optimizer import ComputeOptimizerCollector
from tests.test_compute_optimizer import _FakeCO, _FakeProvider


def _idle(
    resource_id: str,
    resource_type: str,
    *,
    value: float = 10.0,
    finding: str = "Idle",
    metrics: list[dict] | None = None,
) -> dict:
    return {
        "resourceArn": f"arn:aws:x:us-east-1:111:thing/{resource_id}",
        "resourceId": resource_id,
        "resourceType": resource_type,
        "finding": finding,
        "lookBackPeriodInDays": 14.0,
        "utilizationMetrics": metrics or [],
        "savingsOpportunity": {
            "savingsOpportunityPercentage": 100.0,
            "estimatedMonthlySavings": {"currency": "USD", "value": value},
        },
    }


def _recs(**kw):
    return ComputeOptimizerCollector(_FakeProvider(_FakeCO(**kw))).recommendations(None)


def test_every_resource_type_maps_to_a_service():
    idle = {
        "idleRecommendations": [
            _idle("i-1", "EC2Instance"),
            _idle("asg-1", "AutoScalingGroup"),
            _idle("vol-1", "EBSVolume", finding="Unattached"),
            _idle("svc-1", "ECSService"),
            _idle("db-1", "RDSDBInstance"),
            _idle("nat-1", "NatGateway"),
        ]
    }
    recs = _recs(idle=idle)

    by_id = {r.resource.resource_id: r for r in recs}
    assert {r.service for r in recs} == {"ec2", "asg", "ebs", "ecs", "rds"}
    assert by_id["i-1"].service == "ec2"
    assert by_id["db-1"].service == "rds"
    # NAT keeps idle_nat.py's shape so the two sources dedupe against each other
    assert (by_id["nat-1"].service, by_id["nat-1"].kind) == ("ec2", "idle-nat")
    assert {r.kind for r in recs} == {"idle", "idle-nat"}
    assert all(r.resource.region == "us-east-1" for r in recs)


def test_savings_are_reported_where_cloudwatch_could_not():
    [rec] = _recs(idle={"idleRecommendations": [_idle("i-1", "EC2Instance", value=42.5)]})
    assert rec.estimated_savings.amount == Decimal("42.5")
    assert rec.estimated_savings.currency == "USD"


def test_utilization_metrics_land_in_the_summary():
    metrics = [
        {"name": "CPU", "statistic": "Average", "value": 0.4},
        {"name": "NetworkInBytesPerSecond", "statistic": "Average", "value": 12.0},
    ]
    [rec] = _recs(
        idle={"idleRecommendations": [_idle("i-1", "EC2Instance", metrics=metrics)]}
    )
    assert "Idle 14d" in rec.summary
    assert "CPU 0.4" in rec.summary and "NetworkInBytesPerSecond 12" in rec.summary


def test_unknown_resource_type_is_skipped_not_guessed():
    idle = {
        "idleRecommendations": [
            _idle("mystery-1", "SomeFutureType"),
            _idle("i-1", "EC2Instance"),
        ]
    }
    assert [r.resource.resource_id for r in _recs(idle=idle)] == ["i-1"]


def test_resource_id_falls_back_to_the_arn():
    raw = _idle("", "EC2Instance")
    raw["resourceArn"] = "arn:aws:ec2:us-east-1:111:instance/i-from-arn"
    [rec] = _recs(idle={"idleRecommendations": [raw]})
    assert rec.resource.resource_id == "i-from-arn"


def test_idle_recommendations_follow_next_token():
    page1 = {"idleRecommendations": [_idle("i-1", "EC2Instance")], "nextToken": "page2"}
    page2 = {"idleRecommendations": [_idle("i-2", "EC2Instance")]}

    class _PagingCO(_FakeCO):
        def get_idle_recommendations(self, **kw):
            return page2 if kw.get("nextToken") else page1

    recs = ComputeOptimizerCollector(_FakeProvider(_PagingCO())).recommendations(None)
    assert sorted(r.resource.resource_id for r in recs) == ["i-1", "i-2"]


def test_not_enrolled_is_logged_once_and_skipped():
    err = ClientError(
        {"Error": {"Code": "OptInRequiredException", "Message": "not enrolled"}},
        "GetIdleRecommendations",
    )
    calls: list[int] = []

    class _CO(_FakeCO):
        def get_idle_recommendations(self, **kw):
            calls.append(1)
            raise err

    provider = _FakeProvider(_CO())
    collector = ComputeOptimizerCollector(provider)
    assert collector.recommendations(None) == []
    assert "idle" in collector._warned_unavailable
    assert len(calls) == 1


def test_a_failing_idle_call_keeps_the_rightsizing_recs():
    # idle and rightsizing are separate opt-ins; a throttle on one must not
    # discard the other's findings for the region.
    throttle = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "GetIdleRecommendations",
    )

    class _CO(_FakeCO):
        def get_idle_recommendations(self, **kw):
            raise throttle

    ec2 = {
        "instanceRecommendations": [
            {
                "instanceArn": "arn:aws:ec2:us-east-1:111:instance/i-over",
                "currentInstanceType": "m5.4xlarge",
                "finding": "Overprovisioned",
                "recommendationOptions": [
                    {
                        "instanceType": "m5.xlarge",
                        "rank": 1,
                        "savingsOpportunity": {
                            "savingsOpportunityPercentage": 50.0,
                            "estimatedMonthlySavings": {"currency": "USD", "value": 100.0},
                        },
                    }
                ],
            }
        ]
    }
    recs = ComputeOptimizerCollector(_FakeProvider(_CO(ec2=ec2))).recommendations(None)
    assert [r.resource.resource_id for r in recs] == ["i-over"]


def test_idle_rec_flows_to_an_event_keyed_like_the_cloudwatch_one():
    recs = _recs(idle={"idleRecommendations": [_idle("i-1", "EC2Instance")]})
    [event] = RecommendationDetector().detect(recs)
    assert event.key == "finops:rec:prod:aws:ec2:idle:i-1"
