"""The inventory join that replaced Cost Explorer's recommendation APIs.

Also home to the fake provider the two commitment collector tests share — it has
to dispatch on service (ec2 per region + the global savingsplans endpoint), which
is more than the one-client fakes elsewhere need.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from botocore.exceptions import ClientError

from clont.finops.aws import inventory


# --- fakes ------------------------------------------------------------------


class _Paginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **kw):
        return iter(self._pages)


class _FakeEC2:
    def __init__(self, instances: list[dict], reserved: list[dict]) -> None:
        self._instances = instances
        self._reserved = reserved

    def get_paginator(self, name: str):
        assert name == "describe_instances"
        return _Paginator([{"Reservations": [{"Instances": self._instances}]}])

    def describe_reserved_instances(self, **kw):
        return {"ReservedInstances": self._reserved}


class _FakeSavingsPlans:
    def __init__(self, plans: list[dict]) -> None:
        self._plans = plans

    def describe_savings_plans(self, **kw):
        return {"savingsPlans": self._plans}


class _Boom:
    """A region whose ec2 client fails outright."""

    def get_paginator(self, name: str):
        raise ClientError(
            {"Error": {"Code": "UnauthorizedOperation", "Message": "nope"}},
            "DescribeInstances",
        )

    def describe_reserved_instances(self, **kw):
        raise ClientError(
            {"Error": {"Code": "UnauthorizedOperation", "Message": "nope"}},
            "DescribeReservedInstances",
        )


class FakeProvider:
    """Dispatches on service; ec2 is per-region, savingsplans is global."""

    def __init__(
        self,
        *,
        instances: dict[str, list[dict]] | None = None,
        reserved: dict[str, list[dict]] | None = None,
        plans: list[dict] | None = None,
        broken: set[str] | None = None,
        alias: str = "prod",
    ) -> None:
        self._instances = instances or {}
        self._reserved = reserved or {}
        self._plans = plans or []
        self._broken = broken or set()
        self.alias = alias

    def regions(self) -> list[str]:
        return sorted(set(self._instances) | set(self._reserved) | self._broken) or ["us-east-1"]

    def client(self, service: str, region: str | None = None):
        if service == "savingsplans":
            return _FakeSavingsPlans(self._plans)
        assert service == "ec2"
        if region in self._broken:
            return _Boom()
        return _FakeEC2(self._instances.get(region, []), self._reserved.get(region, []))


def instance(iid: str, itype: str = "m5.large", *, az="us-east-1a", state="running", spot=False):
    raw = {
        "InstanceId": iid,
        "InstanceType": itype,
        "State": {"Name": state},
        "Placement": {"AvailabilityZone": az},
    }
    if spot:
        raw["InstanceLifecycle"] = "spot"
    return raw


def reserved(itype: str = "m5.large", count: int = 1, *, scope="Region", az="", state="active"):
    return {
        "ReservedInstancesId": f"ri-{itype}-{az or scope}",
        "InstanceType": itype,
        "InstanceCount": count,
        "State": state,
        "Scope": scope,
        "AvailabilityZone": az,
    }


def plan(commitment: str = "1.00", state: str = "active", plan_type: str = "Compute"):
    return {
        "savingsPlanId": f"sp-{commitment}",
        "state": state,
        "savingsPlanType": plan_type,
        "commitment": commitment,
        "currency": "USD",
    }


def build(provider) -> inventory.Inventory:
    return inventory.build(provider, ttl=0)  # never share a cache entry between tests


@pytest.fixture(autouse=True)
def _no_cache():
    inventory.clear_cache()
    yield
    inventory.clear_cache()


# --- the join ---------------------------------------------------------------


def test_spot_and_stopped_instances_excluded():
    inv = build(FakeProvider(instances={"us-east-1": [
        instance("i-run"),
        instance("i-spot", spot=True),
        instance("i-stopped", state="stopped"),
    ]}))
    assert [r.instance_id for r in inv.running] == ["i-run"]


def test_regional_ri_matches_any_az_in_region():
    inv = build(FakeProvider(
        instances={"us-east-1": [instance("i-1", az="us-east-1b")]},
        reserved={"us-east-1": [reserved(count=1)]},
    ))
    assert inv.matched_count == 1
    assert inv.uncovered == []
    assert inv.ri_coverage_pct == 100


def test_az_scoped_ri_does_not_match_another_az():
    inv = build(FakeProvider(
        instances={"us-east-1": [instance("i-1", az="us-east-1b")]},
        reserved={"us-east-1": [
            reserved(count=1, scope="Availability Zone", az="us-east-1a")
        ]},
    ))
    assert inv.matched_count == 0
    assert [r.instance_id for r in inv.uncovered] == ["i-1"]
    assert inv.unused_reserved == 1


def test_ri_does_not_match_a_different_instance_type():
    inv = build(FakeProvider(
        instances={"us-east-1": [instance("i-1", "c5.xlarge")]},
        reserved={"us-east-1": [reserved("m5.large", count=1)]},
    ))
    assert inv.uncovered and inv.matched_count == 0


def test_az_scoped_pool_binds_before_the_regional_one():
    # One instance in us-east-1a, two pools that could both take it. The az-scoped
    # pool must win, or it would be reported unused while the flexible one is used.
    inv = build(FakeProvider(
        instances={"us-east-1": [instance("i-1", az="us-east-1a")]},
        reserved={"us-east-1": [
            reserved(count=1),
            reserved(count=1, scope="Availability Zone", az="us-east-1a"),
        ]},
    ))
    used = [p for p in inv.pools if p.matched]
    assert len(used) == 1
    assert used[0].az == "us-east-1a"


def test_excess_reservations_are_unused():
    inv = build(FakeProvider(
        instances={"us-east-1": [instance("i-1")]},
        reserved={"us-east-1": [reserved(count=4)]},
    ))
    assert inv.matched_count == 1
    assert inv.unused_reserved == 3
    assert inv.ri_utilization_pct == 25


def test_one_bad_region_is_skipped_not_fatal():
    inv = build(FakeProvider(
        instances={"us-east-1": [instance("i-1")]},
        broken={"eu-west-1"},
    ))
    assert [r.instance_id for r in inv.running] == ["i-1"]


def test_sagemaker_savings_plan_does_not_count_as_compute():
    # It commits against usage we don't inventory; counted, it would look like a
    # compute plan nobody is using.
    inv = build(FakeProvider(
        instances={"us-east-1": [instance("i-1")]},
        plans=[plan("3.00", plan_type="SageMaker"), plan("1.00", plan_type="Compute")],
    ))
    assert inv.committed_hourly == Decimal("1.00")


def test_inactive_savings_plans_and_reservations_ignored():
    inv = build(FakeProvider(
        instances={"us-east-1": [instance("i-1")]},
        reserved={"us-east-1": [reserved(count=2, state="retired")]},
        plans=[plan("1.00", state="payment-failed")],
    ))
    assert inv.reserved_count == 0
    assert inv.committed_hourly == 0


def test_missing_savingsplans_grant_is_not_fatal():
    class _Denied(FakeProvider):
        def client(self, service: str, region: str | None = None):
            if service == "savingsplans":
                raise ClientError(
                    {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                    "DescribeSavingsPlans",
                )
            return super().client(service, region)

    inv = build(_Denied(instances={"us-east-1": [instance("i-1")]}))
    assert inv.plans == []
    assert len(inv.running) == 1


# --- derived figures --------------------------------------------------------


def test_sp_utilization_is_committed_vs_eligible():
    # $2/hr committed, one m5.large (~$0.096/hr) uncovered -> badly under-used.
    inv = build(FakeProvider(
        instances={"us-east-1": [instance("i-1")]},
        plans=[plan("2.00")],
    ))
    assert inv.committed_hourly == Decimal("2.00")
    assert inv.sp_unused_hourly == Decimal("2.00") - inv.uncovered_hourly
    assert 0 < inv.sp_utilization_pct < 10


def test_sp_utilization_caps_at_full():
    # More eligible usage than committed: fully used, never above 100%.
    inv = build(FakeProvider(
        instances={"us-east-1": [instance(f"i-{n}", "m5.24xlarge") for n in range(3)]},
        plans=[plan("1.00")],
    ))
    assert inv.sp_utilization_pct == 100
    assert inv.sp_unused_hourly == 0


def test_no_commitments_held_reads_as_zero_not_an_error():
    inv = build(FakeProvider(instances={"us-east-1": [instance("i-1")]}))
    assert inv.committed_hourly == 0
    assert inv.sp_utilization_pct == 0
    assert inv.reserved_count == 0
    assert inv.ri_utilization_pct == 0


def test_uncovered_hourly_sums_the_price_table():
    from clont.finops.aws import pricing

    inv = build(FakeProvider(instances={"us-east-1": [
        instance("i-1", "m5.large"), instance("i-2", "c5.xlarge"),
    ]}))
    assert inv.uncovered_hourly == (
        pricing.instance_hourly("m5.large") + pricing.instance_hourly("c5.xlarge")
    )
    assert inv.uncovered_monthly == inv.uncovered_hourly * pricing.HOURS_PER_MONTH


def test_build_caches_within_the_ttl():
    provider = FakeProvider(instances={"us-east-1": [instance("i-1")]})
    first = inventory.build(provider)
    provider._instances = {"us-east-1": []}  # a second sweep would see nothing
    assert inventory.build(provider) is first
    assert build(provider).running == []  # ttl=0 forces the re-read
