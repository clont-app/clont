"""Commitment recommendations: Savings Plans + Reserved Instances from CE."""

from __future__ import annotations

from decimal import Decimal

from botocore.exceptions import ClientError

from clont.events.detectors import RecommendationDetector
from clont.finops.aws.commitments import CommitmentsCollector


def _sp_response(monthly: str, hourly: str = "0.50", pct: str = "15") -> dict:
    return {
        "SavingsPlansPurchaseRecommendation": {
            "SavingsPlansPurchaseRecommendationSummary": {
                "EstimatedMonthlySavingsAmount": monthly,
                "EstimatedSavingsPercentage": pct,
                "HourlyCommitmentToPurchase": hourly,
                "CurrencyCode": "USD",
            }
        }
    }


def _ri_response(monthly: str, pct: str = "20") -> dict:
    return {
        "Recommendations": [
            {
                "RecommendationSummary": {
                    "TotalEstimatedMonthlySavingsAmount": monthly,
                    "TotalEstimatedMonthlySavingsPercentage": pct,
                    "CurrencyCode": "USD",
                }
            }
        ]
    }


class _FakeCE:
    def __init__(self, sp: dict | None = None, ri_by_service: dict | None = None) -> None:
        self._sp = sp or {"SavingsPlansPurchaseRecommendation": {}}
        self._ri = ri_by_service or {}

    def get_savings_plans_purchase_recommendation(self, **kw):
        return self._sp

    def get_reservation_purchase_recommendation(self, **kw):
        # Empty "Recommendations" for services we weren't given.
        return self._ri.get(kw["Service"], {"Recommendations": []})


class _FakeProvider:
    def __init__(self, ce: _FakeCE, alias: str = "prod") -> None:
        self._ce = ce
        self.alias = alias

    def client(self, service: str, region: str | None = None):
        assert service == "ce"
        return self._ce


def test_savings_plan_recommendation_emitted():
    recs = CommitmentsCollector(
        _FakeProvider(_FakeCE(sp=_sp_response("120.00")))
    ).recommendations(None)

    sp = [r for r in recs if r.service == "savings-plans"]
    assert len(sp) == 1
    [rec] = sp
    assert rec.kind == "savings-plan"
    assert rec.resource.resource_id == "COMPUTE_SP"
    assert rec.estimated_savings.amount == Decimal("120.00")
    assert "Savings Plan" in rec.summary


def test_reserved_instances_per_service():
    ri = {
        "Amazon Elastic Compute Cloud - Compute": _ri_response("200.00"),
        "Amazon Relational Database Service": _ri_response("50.00"),
    }
    recs = CommitmentsCollector(_FakeProvider(_FakeCE(ri_by_service=ri))).recommendations(None)

    ris = {r.resource.resource_id: r for r in recs if r.service == "reserved-instances"}
    assert set(ris) == {"ec2", "rds"}
    assert ris["ec2"].estimated_savings.amount == Decimal("200.00")
    assert ris["ec2"].kind == "reserved-instance"


def test_zero_savings_skipped():
    recs = CommitmentsCollector(
        _FakeProvider(_FakeCE(sp=_sp_response("0")))
    ).recommendations(None)
    assert recs == []


def test_data_unavailable_returns_empty():
    err = ClientError(
        {"Error": {"Code": "DataUnavailableException", "Message": "not enough data"}},
        "GetSavingsPlansPurchaseRecommendation",
    )

    class _Deny(_FakeCE):
        def get_savings_plans_purchase_recommendation(self, **kw):
            raise err

        def get_reservation_purchase_recommendation(self, **kw):
            raise err

    recs = CommitmentsCollector(_FakeProvider(_Deny())).recommendations(None)
    assert recs == []


def test_savings_plans_error_still_yields_reserved_instances():
    # An unexpected error on the Savings Plans call must not drop the RI recs.
    boom = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "GetSavingsPlansPurchaseRecommendation",
    )

    class _CE(_FakeCE):
        def get_savings_plans_purchase_recommendation(self, **kw):
            raise boom

    ri = {"Amazon Elastic Compute Cloud - Compute": _ri_response("200.00")}
    recs = CommitmentsCollector(_FakeProvider(_CE(ri_by_service=ri))).recommendations(None)
    assert [r.service for r in recs] == ["reserved-instances"]
    assert recs[0].resource.resource_id == "ec2"


def test_commitment_event_key_includes_kind():
    recs = CommitmentsCollector(
        _FakeProvider(_FakeCE(sp=_sp_response("120.00")))
    ).recommendations(None)
    events = RecommendationDetector().detect(recs)
    keys = {e.key for e in events}
    assert "finops:rec:prod:aws:savings-plans:savings-plan:COMPUTE_SP" in keys
