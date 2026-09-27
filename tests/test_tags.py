"""Tag-hygiene: cost-bearing resources missing required tags."""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

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


class _FakeRDS:
    def __init__(self, databases: list[dict]) -> None:
        self._databases = databases

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "describe_db_instances"
        return _Paginator([{"DBInstances": self._databases}])


class _FakeELB:
    def __init__(self, load_balancers: list[dict], tags: dict[str, dict[str, str]]) -> None:
        self._lbs = load_balancers
        self._tags = tags
        self.tag_calls: list[list[str]] = []

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "describe_load_balancers"
        return _Paginator([{"LoadBalancers": self._lbs}])

    def describe_tags(self, ResourceArns: list[str]):  # noqa: N803 - boto3 spelling
        self.tag_calls.append(ResourceArns)
        return {
            "TagDescriptions": [
                {
                    "ResourceArn": arn,
                    "Tags": [{"Key": k, "Value": v} for k, v in self._tags.get(arn, {}).items()],
                }
                for arn in ResourceArns
            ]
        }


class _FakeLambda:
    def __init__(self, functions: list[dict], tags: dict[str, dict[str, str]]) -> None:
        self._functions = functions
        self._tags = tags

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "list_functions"
        return _Paginator([{"Functions": self._functions}])

    def list_tags(self, Resource: str):  # noqa: N803
        return {"Tags": self._tags.get(Resource, {})}


class _FakeS3:
    def __init__(self, buckets: dict[str, dict], region: str | None = None) -> None:
        # name -> {"tags": {...}} ; a bucket with no "tags" key has no tag set
        self._buckets = buckets
        self._region = region

    def list_buckets(self):
        return {"Buckets": [{"Name": n} for n in self._buckets]}

    def get_bucket_location(self, Bucket: str):  # noqa: N803
        return {"LocationConstraint": self._region}

    def get_bucket_tagging(self, Bucket: str):  # noqa: N803
        bucket = self._buckets[Bucket]
        if "tags" not in bucket:
            raise ClientError({"Error": {"Code": "NoSuchTagSet"}}, "GetBucketTagging")
        return {"TagSet": [{"Key": k, "Value": v} for k, v in bucket["tags"].items()]}


class _FakeProvider:
    def __init__(self, alias: str = "prod", **clients) -> None:
        self.alias = alias
        self._clients = clients

    def regions(self) -> list[str]:
        return ["us-east-1"]

    def client(self, service: str, region: str | None = None):
        try:
            return self._clients[service]
        except KeyError:
            raise AssertionError(f"unexpected client: {service}") from None


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


def _db(did: str, **tags) -> dict:
    return {
        "DBInstanceIdentifier": did,
        "DBInstanceStatus": "available",
        "TagList": [{"Key": k, "Value": v} for k, v in tags.items()],
    }


def _lb(name: str) -> dict:
    return {
        "LoadBalancerArn": f"arn:aws:elb:::{name}",
        "LoadBalancerName": name,
        "Type": "application",
        "State": {"Code": "active"},
    }


def _provider(**kw) -> _FakeProvider:
    """A provider whose every service answers, empty unless the test fills it."""
    return _FakeProvider(
        ec2=_FakeEC2(kw.get("instances", []), kw.get("volumes", [])),
        rds=_FakeRDS(kw.get("databases", [])),
        elbv2=_FakeELB(kw.get("load_balancers", []), kw.get("elb_tags", {})),
        **{"lambda": _FakeLambda(kw.get("functions", []), kw.get("function_tags", {}))},
        s3=_FakeS3(kw.get("buckets", {})),
    )


def _collect(required=_REQUIRED, **kw):
    coll = TagHygieneCollector(_provider(**kw), FinOpsTuning(required_tags=required))
    return coll.recommendations(None)


def test_no_required_tags_is_a_noop():
    coll = TagHygieneCollector(_provider(instances=[_inst("i-1")]), FinOpsTuning())
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


def test_rds_instance_flagged_from_its_taglist():
    [rec] = _collect(databases=[_db("db-bad", Owner="alice")])
    assert (rec.service, rec.resource.resource_id) == ("rds", "db-bad")
    assert "Environment" in rec.summary


def test_tagged_rds_instance_passes():
    assert _collect(databases=[_db("db-ok", Owner="a", Environment="prod")]) == []


def test_load_balancer_tags_come_from_describe_tags():
    arn = "arn:aws:elb:::lb-bad"
    recs = _collect(
        load_balancers=[_lb("lb-ok"), _lb("lb-bad")],
        elb_tags={
            "arn:aws:elb:::lb-ok": {"Owner": "a", "Environment": "prod"},
            arn: {"Owner": "a"},
        },
    )

    assert [(r.service, r.resource.resource_id) for r in recs] == [("elb", "lb-bad")]


def test_load_balancers_are_batched_within_the_api_limit():
    lbs = [_lb(f"lb-{i}") for i in range(25)]
    provider = _provider(load_balancers=lbs)
    TagHygieneCollector(provider, FinOpsTuning(required_tags=_REQUIRED)).recommendations(None)

    assert [len(batch) for batch in provider._clients["elbv2"].tag_calls] == [20, 5]


def test_lambda_function_flagged():
    arn = "arn:aws:lambda:::function:fn-bad"
    [rec] = _collect(
        functions=[{"FunctionArn": arn, "FunctionName": "fn-bad"}],
        function_tags={arn: {"Owner": "a"}},
    )
    assert (rec.service, rec.resource.resource_id) == ("lambda", "fn-bad")
    assert "Environment" in rec.summary


def test_bucket_with_no_tag_set_is_flagged_once():
    recs = _collect(
        buckets={"b-bad": {}, "b-ok": {"tags": {"Owner": "a", "Environment": "prod"}}}
    )

    assert [(r.service, r.resource.resource_id) for r in recs] == [("s3", "b-bad")]
    assert recs[0].resource.region == "us-east-1"  # null LocationConstraint


def test_buckets_are_only_walked_once_across_regions():
    provider = _provider(buckets={"b-bad": {}})
    provider.regions = lambda: ["us-east-1", "eu-west-1"]

    recs = TagHygieneCollector(
        provider, FinOpsTuning(required_tags=_REQUIRED)
    ).recommendations(None)

    assert len(recs) == 1


def test_one_denied_service_does_not_sink_the_others(caplog):
    class _Denied:
        def get_paginator(self, name: str):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "ListFunctions")

    provider = _provider(instances=[_inst("i-bad")])
    provider._clients["lambda"] = _Denied()

    with caplog.at_level("WARNING"):
        recs = TagHygieneCollector(
            provider, FinOpsTuning(required_tags=_REQUIRED)
        ).recommendations(None)

    assert [r.resource.resource_id for r in recs] == ["i-bad"]
    assert "skipping lambda" in caplog.text


@pytest.mark.parametrize(
    ("constraint", "expected"), [(None, "us-east-1"), ("EU", "eu-west-1"), ("ap-south-1", "ap-south-1")]
)
def test_bucket_region_spellings(constraint, expected):
    provider = _FakeProvider(
        ec2=_FakeEC2([], []),
        rds=_FakeRDS([]),
        elbv2=_FakeELB([], {}),
        **{"lambda": _FakeLambda([], {})},
        s3=_FakeS3({"b": {}}, region=constraint),
    )

    [rec] = TagHygieneCollector(
        provider, FinOpsTuning(required_tags=_REQUIRED)
    ).recommendations(None)

    assert rec.resource.region == expected
