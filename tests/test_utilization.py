"""Commitment utilization & coverage recommendations from Cost Explorer."""

from __future__ import annotations

from decimal import Decimal

from botocore.exceptions import ClientError

from clont.finops.aws.utilization import CommitmentUtilizationCollector
from clont.finops.base import FinOpsTuning


def _sp_util(pct: str, total: str = "1000", unused: str = "0") -> dict:
    return {"Total": {
        "Utilization": {
            "UtilizationPercentage": pct,
            "TotalCommitment": total,
            "UnusedCommitment": unused,
        },
        "Savings": {"CurrencyCode": "USD"},
    }}


def _sp_cov(pct: str, on_demand: str = "500") -> dict:
    return {"SavingsPlansCoverages": [
        {"Coverage": {"CoveragePercentage": pct, "OnDemandCost": on_demand}}
    ]}


def _ri_util(pct: str, purchased: str = "1000", unused: str = "0") -> dict:
    return {"Total": {
        "UtilizationPercentage": pct,
        "PurchasedHours": purchased,
        "UnusedHours": unused,
    }}


def _ri_cov(pct: str, on_demand: str = "100") -> dict:
    return {"Total": {"CoverageHours": {
        "CoverageHoursPercentage": pct,
        "OnDemandHours": on_demand,
    }}}


class _FakeCE:
    def __init__(self, sp_util=None, sp_cov=None, ri_util=None, ri_cov=None) -> None:
        self._sp_util = sp_util or _sp_util("100", total="0")   # no SP held
        self._sp_cov = sp_cov or {"SavingsPlansCoverages": []}
        self._ri_util = ri_util or _ri_util("100", purchased="0")  # no RIs held
        self._ri_cov = ri_cov or {"Total": {}}

    def get_savings_plans_utilization(self, **kw):
        return self._sp_util

    def get_savings_plans_coverage(self, **kw):
        return self._sp_cov

    def get_reservation_utilization(self, **kw):
        return self._ri_util

    def get_reservation_coverage(self, **kw):
        return self._ri_cov


class _FakeProvider:
    def __init__(self, ce: _FakeCE, alias: str = "prod") -> None:
        self._ce = ce
        self.alias = alias

    def client(self, service: str, region: str | None = None):
        assert service == "ce"
        return self._ce


def _collect(ce: _FakeCE, **tuning):
    coll = CommitmentUtilizationCollector(_FakeProvider(ce), FinOpsTuning(**tuning))
    return coll.recommendations(None)


def test_low_sp_utilization_flagged():
    recs = _collect(_FakeCE(sp_util=_sp_util("60", total="1000", unused="400")))
    [rec] = [r for r in recs if r.kind == "low-sp-utilization"]
    assert rec.service == "savings-plans"
    assert rec.estimated_savings.amount == Decimal("400")
    assert "60%" in rec.summary


def test_high_sp_utilization_silent():
    recs = _collect(_FakeCE(sp_util=_sp_util("95", total="1000", unused="50")))
    assert [r for r in recs if r.kind == "low-sp-utilization"] == []


def test_no_commitment_held_is_silent():
    # Defaults: TotalCommitment 0 and PurchasedHours 0 -> nothing to judge.
    assert _collect(_FakeCE()) == []


def test_low_sp_coverage_flagged():
    recs = _collect(_FakeCE(sp_cov=_sp_cov("40", on_demand="500")))
    [rec] = [r for r in recs if r.kind == "low-sp-coverage"]
    assert "40%" in rec.summary
    assert "500" in rec.summary


def test_coverage_silent_without_on_demand():
    recs = _collect(_FakeCE(sp_cov=_sp_cov("0", on_demand="0")))
    assert [r for r in recs if r.kind == "low-sp-coverage"] == []


def test_low_ri_utilization_flagged():
    recs = _collect(_FakeCE(ri_util=_ri_util("70", purchased="1000", unused="300")))
    [rec] = [r for r in recs if r.kind == "low-ri-utilization"]
    assert rec.service == "reserved-instances"
    assert "300" in rec.summary


def test_low_ri_coverage_flagged():
    recs = _collect(_FakeCE(ri_cov=_ri_cov("50", on_demand="80")))
    [rec] = [r for r in recs if r.kind == "low-ri-coverage"]
    assert "50%" in rec.summary


def test_thresholds_are_configurable():
    # 85% utilization passes the default 90 gate as "low" but a 80 gate clears it.
    ce = _FakeCE(sp_util=_sp_util("85", total="1000", unused="150"))
    assert [r for r in _collect(ce) if r.kind == "low-sp-utilization"]
    assert _collect(ce, ri_sp_min_utilization=80.0) == []


def test_one_failing_query_does_not_drop_others():
    boom = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow"}},
        "GetSavingsPlansUtilization",
    )

    class _CE(_FakeCE):
        def get_savings_plans_utilization(self, **kw):
            raise boom

    recs = _collect(_CE(ri_util=_ri_util("70", purchased="1000", unused="300")))
    assert [r.kind for r in recs] == ["low-ri-utilization"]


def test_data_unavailable_swallowed():
    err = ClientError(
        {"Error": {"Code": "DataUnavailableException", "Message": "no data"}},
        "GetReservationUtilization",
    )

    class _CE(_FakeCE):
        def get_savings_plans_utilization(self, **kw):
            raise err

        def get_savings_plans_coverage(self, **kw):
            raise err

        def get_reservation_utilization(self, **kw):
            raise err

        def get_reservation_coverage(self, **kw):
            raise err

    assert _collect(_CE()) == []
