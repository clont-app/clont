"""Compute Optimizer recommendations: savings filtering + rank selection."""

from __future__ import annotations

from decimal import Decimal

from botocore.exceptions import ClientError

from clont.events.detectors import RecommendationDetector
from clont.finops.aws.compute_optimizer import ComputeOptimizerCollector


def _opt(instance_type: str, rank: int, value: float, pct: float = 0.0) -> dict:
    return {
        "instanceType": instance_type,
        "rank": rank,
        "savingsOpportunity": {
            "savingsOpportunityPercentage": pct,
            "estimatedMonthlySavings": {"currency": "USD", "value": value},
        },
    }


class _FakeCO:
    def __init__(
        self,
        ec2: dict | None = None,
        ebs: dict | None = None,
        asg: dict | None = None,
        lambda_: dict | None = None,
        ecs: dict | None = None,
        rds: dict | None = None,
    ) -> None:
        self._ec2 = ec2 or {"instanceRecommendations": []}
        self._ebs = ebs or {"volumeRecommendations": []}
        self._asg = asg or {"autoScalingGroupRecommendations": []}
        self._lambda = lambda_ or {"lambdaFunctionRecommendations": []}
        self._ecs = ecs or {"ecsServiceRecommendations": []}
        self._rds = rds or {"rdsDBRecommendations": []}

    def get_ec2_instance_recommendations(self, **kw):
        return self._ec2

    def get_ebs_volume_recommendations(self, **kw):
        return self._ebs

    def get_auto_scaling_group_recommendations(self, **kw):
        return self._asg

    def get_lambda_function_recommendations(self, **kw):
        return self._lambda

    def get_ecs_service_recommendations(self, **kw):
        return self._ecs

    def get_rds_database_recommendations(self, **kw):
        return self._rds


class _FakeProvider:
    def __init__(self, co: _FakeCO, alias: str = "prod") -> None:
        self._co = co
        self.alias = alias

    def regions(self) -> list[str]:
        return ["us-east-1"]

    def client(self, service: str, region: str | None = None):
        assert service == "compute-optimizer"
        return self._co


def test_ec2_emits_only_savings_and_picks_best_rank():
    ec2 = {
        "instanceRecommendations": [
            {
                "instanceArn": "arn:aws:ec2:us-east-1:111:instance/i-over",
                "instanceName": "web",
                "currentInstanceType": "m5.4xlarge",
                "finding": "Overprovisioned",
                "recommendationOptions": [
                    _opt("m5.2xlarge", rank=2, value=80.0, pct=40.0),
                    _opt("m5.xlarge", rank=1, value=100.0, pct=50.0),  # best (rank 1)
                ],
            },
            {
                "instanceArn": "arn:aws:ec2:us-east-1:111:instance/i-ok",
                "currentInstanceType": "m5.large",
                "finding": "Optimized",
                "recommendationOptions": [_opt("m5.large", rank=1, value=0.0)],
            },
        ]
    }
    recs = ComputeOptimizerCollector(_FakeProvider(_FakeCO(ec2=ec2))).recommendations(None)

    assert len(recs) == 1  # the Optimized / zero-savings instance is skipped
    [rec] = recs
    assert rec.service == "ec2"
    assert rec.resource.resource_id == "i-over"
    assert rec.resource.region == "us-east-1"
    assert rec.estimated_savings.amount == Decimal("100")  # rank-1 option, not first-listed
    assert rec.estimated_savings.currency == "USD"
    assert "Overprovisioned" in rec.summary and "m5.xlarge" in rec.summary


def test_ebs_recommendation_emitted():
    ebs = {
        "volumeRecommendations": [
            {
                "volumeArn": "arn:aws:ec2:us-east-1:111:volume/vol-123",
                "finding": "NotOptimized",
                "volumeRecommendationOptions": [_opt("", rank=1, value=5.0, pct=20.0)],
            }
        ]
    }
    [rec] = ComputeOptimizerCollector(_FakeProvider(_FakeCO(ebs=ebs))).recommendations(None)
    assert rec.service == "ebs"
    assert rec.resource.resource_id == "vol-123"
    assert rec.estimated_savings.amount == Decimal("5")


def test_recommendation_flows_to_event_with_alias_key():
    ec2 = {
        "instanceRecommendations": [
            {
                "instanceArn": "arn:aws:ec2:us-east-1:111:instance/i-over",
                "currentInstanceType": "m5.4xlarge",
                "finding": "Overprovisioned",
                "recommendationOptions": [_opt("m5.xlarge", rank=1, value=100.0, pct=50.0)],
            }
        ]
    }
    recs = ComputeOptimizerCollector(_FakeProvider(_FakeCO(ec2=ec2))).recommendations(None)
    [event] = RecommendationDetector().detect(recs)
    assert event.key == "finops:rec:prod:aws:ec2:rightsize:i-over"
    assert event.title.startswith("[prod]")


def test_no_recommendations_when_nothing_to_save():
    recs = ComputeOptimizerCollector(_FakeProvider(_FakeCO())).recommendations(None)
    assert recs == []


def test_recommendations_follow_next_token():
    def _inst(iid: str) -> dict:
        return {
            "instanceArn": f"arn:aws:ec2:us-east-1:111:instance/{iid}",
            "currentInstanceType": "m5.2xlarge",
            "finding": "Overprovisioned",
            "recommendationOptions": [_opt("m5.large", rank=1, value=40.0, pct=30.0)],
        }

    page1 = {"instanceRecommendations": [_inst("i-1")], "nextToken": "page2"}
    page2 = {"instanceRecommendations": [_inst("i-2")]}  # no nextToken -> last page

    class _PagingCO(_FakeCO):
        def get_ec2_instance_recommendations(self, **kw):
            return page2 if kw.get("nextToken") else page1

    recs = ComputeOptimizerCollector(_FakeProvider(_PagingCO())).recommendations(None)
    assert sorted(r.resource.resource_id for r in recs) == ["i-1", "i-2"]


def test_not_enrolled_returns_empty():
    err = ClientError(
        {"Error": {"Code": "OptInRequiredException", "Message": "not enrolled"}},
        "GetEC2InstanceRecommendations",
    )

    class _Deny:
        def __getattr__(self, _name):  # every get_*_recommendations call denies
            def _raise(**kw):
                raise err
            return _raise

    recs = ComputeOptimizerCollector(_FakeProvider(_Deny())).recommendations(None)
    assert recs == []


def test_other_types_still_collected_when_one_is_not_enrolled():
    # RDS recommendations are denied (separate opt-in) but EC2 is enrolled — the
    # EC2 rec must survive, not be suppressed by the RDS denial.
    rds_err = ClientError(
        {"Error": {"Code": "OptInRequiredException", "Message": "rds not enrolled"}},
        "GetRDSDatabaseRecommendations",
    )

    class _CO(_FakeCO):
        def get_rds_database_recommendations(self, **kw):
            raise rds_err

    ec2 = {
        "instanceRecommendations": [
            {
                "instanceArn": "arn:aws:ec2:us-east-1:111:instance/i-over",
                "currentInstanceType": "m5.4xlarge",
                "finding": "Overprovisioned",
                "recommendationOptions": [_opt("m5.xlarge", rank=1, value=100.0, pct=50.0)],
            }
        ]
    }
    recs = ComputeOptimizerCollector(_FakeProvider(_CO(ec2=ec2))).recommendations(None)
    assert [r.resource.resource_id for r in recs] == ["i-over"]


def test_transient_error_on_one_source_keeps_the_others():
    # A throttle (not an opt-in error) on Lambda must not discard the EC2 rec
    # already gathered in the same region.
    throttle = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "GetLambdaFunctionRecommendations",
    )

    class _CO(_FakeCO):
        def get_lambda_function_recommendations(self, **kw):
            raise throttle

    ec2 = {
        "instanceRecommendations": [
            {
                "instanceArn": "arn:aws:ec2:us-east-1:111:instance/i-over",
                "currentInstanceType": "m5.4xlarge",
                "finding": "Overprovisioned",
                "recommendationOptions": [_opt("m5.xlarge", rank=1, value=100.0, pct=50.0)],
            }
        ]
    }
    recs = ComputeOptimizerCollector(_FakeProvider(_CO(ec2=ec2))).recommendations(None)
    assert [r.resource.resource_id for r in recs] == ["i-over"]


def test_asg_lambda_ecs_rds_recommendations_emitted():
    asg = {
        "autoScalingGroupRecommendations": [
            {
                "autoScalingGroupArn": "arn:aws:autoscaling:us-east-1:111:autoScalingGroup:uuid:autoScalingGroupName/web-asg",
                "finding": "NotOptimized",
                "recommendationOptions": [_opt("", rank=1, value=30.0, pct=25.0)],
            }
        ]
    }
    lambda_ = {
        "lambdaFunctionRecommendations": [
            {
                "functionArn": "arn:aws:lambda:us-east-1:111:function:my-func",
                "finding": "NotOptimized",
                "memorySizeRecommendationOptions": [_opt("", rank=1, value=4.0, pct=15.0)],
            }
        ]
    }
    ecs = {
        "ecsServiceRecommendations": [
            {
                "serviceArn": "arn:aws:ecs:us-east-1:111:service/cluster/api-svc",
                "finding": "Underprovisioned",
                "recommendationOptions": [_opt("", rank=1, value=12.0, pct=10.0)],
            }
        ]
    }
    rds = {
        "rdsDBRecommendations": [
            {
                "resourceArn": "arn:aws:rds:us-east-1:111:db:prod-db",
                "instanceFinding": "Overprovisioned",
                "instanceRecommendationOptions": [_opt("", rank=1, value=80.0, pct=40.0)],
            }
        ]
    }
    recs = ComputeOptimizerCollector(
        _FakeProvider(_FakeCO(asg=asg, lambda_=lambda_, ecs=ecs, rds=rds))
    ).recommendations(None)

    by_service = {r.service: r for r in recs}
    assert set(by_service) == {"asg", "lambda", "ecs", "rds"}
    assert by_service["asg"].resource.resource_id == "web-asg"
    assert by_service["lambda"].resource.resource_id == "my-func"   # ARN uses ':'
    assert by_service["ecs"].resource.resource_id == "api-svc"
    assert by_service["rds"].resource.resource_id == "prod-db"      # ARN uses ':'
    assert by_service["rds"].estimated_savings.amount == Decimal("80")
    assert all(r.kind == "rightsize" for r in recs)
