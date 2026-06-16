"""Tag-hygiene: cost-bearing resources missing required tags."""

from __future__ import annotations

from clont.finops.aws.tags import TagHygieneCollector
from clont.finops.base import FinOpsTuning

_REQUIRED = ("Owner", "Environment")


class _Paginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **kw):
        yield from self._pages


class _FakeEC2:
    def __init__(self, instances: list[dict], volumes: list[dict]) -> None:
        self._instances = instances
        self._volumes = volumes

    def get_paginator(self, name: str) -> _Paginator:
        if name == "describe_instances":
            return _Paginator([{"Reservations": [{"Instances": self._instances}]}])
        assert name == "describe_volumes"
        return _Paginator([{"Volumes": self._volumes}])


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


def _vol(vid: str, **tags) -> dict:
    return {
        "VolumeId": vid,
        "Size": 10,
        "VolumeType": "gp3",
        "State": "in-use",
        "Tags": [{"Key": k, "Value": v} for k, v in tags.items()],
    }


def _collect(instances=None, volumes=None, required=_REQUIRED):
    coll = TagHygieneCollector(
        _FakeProvider(_FakeEC2(instances or [], volumes or [])),
        FinOpsTuning(required_tags=required),
    )
    return coll.recommendations(None)


def test_no_required_tags_is_a_noop():
    coll = TagHygieneCollector(
        _FakeProvider(_FakeEC2([_inst("i-1")], [])), FinOpsTuning()
    )
    assert coll.recommendations(None) == []


def test_fully_tagged_resource_passes():
    recs = _collect(
        instances=[_inst("i-ok", Owner="alice", Environment="prod")],
        volumes=[_vol("vol-ok", Owner="bob", Environment="prod")],
    )
    assert recs == []


def test_instance_missing_tags_flagged():
    [rec] = _collect(instances=[_inst("i-bad", Owner="alice")])  # no Environment
    assert rec.resource.resource_id == "i-bad"
    assert rec.service == "ec2"
    assert rec.kind == "missing-tags"
    assert "Environment" in rec.summary
    assert "Owner" not in rec.summary


def test_volume_missing_tags_flagged():
    [rec] = _collect(volumes=[_vol("vol-bad")])  # no tags at all
    assert rec.resource.resource_id == "vol-bad"
    assert rec.service == "ebs"
    assert "Owner" in rec.summary and "Environment" in rec.summary


def test_blank_tag_value_counts_as_missing():
    [rec] = _collect(instances=[_inst("i-blank", Owner="", Environment="prod")])
    assert "Owner" in rec.summary


def test_terminated_instance_skipped():
    assert _collect(instances=[_inst("i-gone", state="terminated")]) == []


def test_missing_keys_listed_in_declared_order():
    [rec] = _collect(instances=[_inst("i-none")])  # both missing
    assert "Owner, Environment" in rec.summary
