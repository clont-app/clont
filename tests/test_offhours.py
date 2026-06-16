"""Off-hours scheduling recommendations for always-on non-prod instances."""

from __future__ import annotations

from decimal import Decimal

from clont.finops.aws.offhours import OffHoursCollector
from clont.finops.base import FinOpsTuning

_NONPROD = {"Environment": ("dev", "staging", "test", "qa")}


class _Paginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **kw):
        yield from self._pages


class _FakeEC2:
    def __init__(self, instances: list[dict]) -> None:
        self._instances = instances

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "describe_instances"
        return _Paginator([{"Reservations": [{"Instances": self._instances}]}])


class _FakeProvider:
    def __init__(self, ec2: _FakeEC2, alias: str = "prod") -> None:
        self._ec2 = ec2
        self.alias = alias

    def regions(self) -> list[str]:
        return ["us-east-1"]

    def client(self, service: str, region: str | None = None):
        assert service == "ec2"
        return self._ec2


def _inst(iid: str, state: str = "running", **tags) -> dict:
    return {
        "InstanceId": iid,
        "State": {"Name": state},
        "Tags": [{"Key": k, "Value": v} for k, v in tags.items()],
    }


def _collect(instances: list[dict], nonprod=_NONPROD):
    coll = OffHoursCollector(
        _FakeProvider(_FakeEC2(instances)), FinOpsTuning(nonprod_tags=nonprod)
    )
    return coll.recommendations(None)


def test_no_config_is_a_noop():
    coll = OffHoursCollector(_FakeProvider(_FakeEC2([_inst("i-1", Environment="dev")])))
    assert coll.recommendations(None) == []


def test_running_nonprod_instance_flagged():
    [rec] = _collect([_inst("i-dev", Environment="dev")])
    assert rec.resource.resource_id == "i-dev"
    assert rec.kind == "off-hours-schedule"
    assert rec.service == "ec2"
    assert rec.estimated_savings.amount == Decimal(0)
    assert "Environment=dev" in rec.summary


def test_prod_instance_not_flagged():
    assert _collect([_inst("i-prod", Environment="production")]) == []


def test_stopped_instance_not_flagged():
    assert _collect([_inst("i-dev", state="stopped", Environment="dev")]) == []


def test_untagged_instance_not_flagged():
    assert _collect([_inst("i-bare")]) == []


def test_value_match_is_case_insensitive():
    [rec] = _collect([_inst("i-dev", Environment="DEV")])
    assert rec.resource.resource_id == "i-dev"


def test_mixed_fleet():
    instances = [
        _inst("i-dev", Environment="dev"),
        _inst("i-stg", Environment="staging"),
        _inst("i-prod", Environment="prod"),
        _inst("i-dev-stopped", state="stopped", Environment="dev"),
    ]
    ids = {r.resource.resource_id for r in _collect(instances)}
    assert ids == {"i-dev", "i-stg"}
