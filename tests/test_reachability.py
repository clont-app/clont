"""ami reachability gc: edges from moto, LastLaunchedTime and age stubbed in"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from clont.finops.aws import pricing
from clont.finops.aws.reachability import BACKUP_TAG, DLM_TAG, KEEP_TAG, ReachabilityCollector
from clont.finops.base import FinOpsTuning

REGION = "us-east-1"


def _iso(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class _Pages:
    def __init__(self, paginator, patch: dict[str, dict]) -> None:
        self._paginator = paginator
        self._patch = patch

    def paginate(self, **kw):
        for page in self._paginator.paginate(**kw):
            for img in page.get("Images", []):
                # moto never fills LastLaunchedTime and stamps CreationDate now
                img["CreationDate"] = _iso(400)
                img.update(self._patch.get(img["ImageId"], {}))
            yield page


class _Patched:
    def __init__(self, client, patch: dict[str, dict] | None = None, fail: dict | None = None):
        self._client = client
        self._patch = patch or {}
        self._fail = fail or {}

    def __getattr__(self, name):
        return getattr(self._client, name)

    def get_paginator(self, name: str):
        if name in self._fail:
            raise self._fail[name]
        pag = self._client.get_paginator(name)
        return _Pages(pag, self._patch) if name == "describe_images" else pag


class _Backup:
    """moto has no recovery points, so a vault -> ami ids stub"""

    def __init__(self, points: dict[str, list[str]] | None = None, fail=None) -> None:
        self._points = points or {}
        self._fail = fail

    def get_paginator(self, name: str):
        if self._fail:
            raise self._fail
        return self

    def paginate(self, BackupVaultName=None, **kw):
        if BackupVaultName is None:
            yield {"BackupVaultList": [{"BackupVaultName": v} for v in self._points]}
            return
        arns = [f"arn:aws:ec2:{REGION}::image/{a}" for a in self._points[BackupVaultName]]
        yield {"RecoveryPoints": [{"RecoveryPointArn": a} for a in arns]}


class _Provider:
    alias = "prod"

    def __init__(self, ec2, asg, backup=None) -> None:
        self._clients = {"ec2": ec2, "autoscaling": asg, "backup": backup or _Backup()}

    def regions(self) -> list[str]:
        return [REGION]

    def client(self, service: str, region: str | None = None):
        return self._clients[service]


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        yield boto3.client("ec2", REGION), boto3.client("autoscaling", REGION)


def _ami(ec2, name: str = "golden") -> str:
    return ec2.register_image(Name=name, RootDeviceName="/dev/xvda")["ImageId"]


def _recs(aws, patch=None, fail=None, asg_fail=None, tuning=None, backup=None):
    ec2, asg = aws
    provider = _Provider(_Patched(ec2, patch, fail), _Patched(asg, fail=asg_fail), backup)
    return ReachabilityCollector(provider, tuning).recommendations(None)


def _denied(op: str, msg: str = "not authorized") -> ClientError:
    return ClientError({"Error": {"Code": "UnauthorizedOperation", "Message": msg}}, op)


def test_unreached_ami_is_an_orphan_with_its_path(aws):
    ec2, _ = aws
    ami = _ami(ec2)
    [rec] = _recs(aws, patch={ami: {"LastLaunchedTime": _iso(200)}})

    assert rec.kind == "orphaned-ami"
    assert rec.resource.resource_id == ami
    assert "no instance, no launch template version, no launch configuration" in rec.summary
    assert "last launched 200d ago" in rec.summary


def test_never_launched_says_so(aws):
    ec2, _ = aws
    _ami(ec2)
    [rec] = _recs(aws)
    assert "never launched (or not since 2017-04" in rec.summary


def test_running_instance_keeps_it_live(aws):
    ec2, _ = aws
    ami = _ami(ec2)
    ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1)
    assert _recs(aws) == []


def test_stopped_instance_is_a_root(aws):
    ec2, _ = aws
    ami = _ami(ec2)
    iid = ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]
    ec2.stop_instances(InstanceIds=[iid])
    assert _recs(aws) == []


def test_terminated_instance_is_no_edge(aws):
    ec2, _ = aws
    ami = _ami(ec2)
    iid = ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]
    ec2.terminate_instances(InstanceIds=[iid])
    assert [r.resource.resource_id for r in _recs(aws)] == [ami]


def test_any_launch_template_version_keeps_it_live(aws):
    ec2, _ = aws
    ami = _ami(ec2)
    ec2.create_launch_template(LaunchTemplateName="web", LaunchTemplateData={"ImageId": "ami-other"})
    # an old non-default version still counts, anyone can launch it
    ec2.create_launch_template_version(LaunchTemplateName="web", LaunchTemplateData={"ImageId": ami})
    assert _recs(aws) == []


def test_launch_configuration_keeps_it_live(aws):
    ec2, asg = aws
    ami = _ami(ec2)
    asg.create_launch_configuration(
        LaunchConfigurationName="legacy", ImageId=ami, InstanceType="t3.micro"
    )
    assert _recs(aws) == []


def test_shared_ami_is_a_root(aws):
    ec2, _ = aws
    ami = _ami(ec2)
    ec2.modify_image_attribute(
        ImageId=ami, LaunchPermission={"Add": [{"UserId": "111122223333"}]}
    )
    assert _recs(aws) == []


def test_public_ami_is_a_root(aws):
    ec2, _ = aws
    ami = _ami(ec2)
    assert _recs(aws, patch={ami: {"Public": True}}) == []


def test_dlm_tag_on_the_ami_is_a_root(aws):
    ec2, _ = aws
    ami = _ami(ec2)
    ec2.create_tags(Resources=[ami], Tags=[{"Key": DLM_TAG, "Value": "policy-1"}])
    assert _recs(aws) == []


def test_keep_tag_is_a_root(aws):
    ec2, _ = aws
    ami = _ami(ec2)
    ec2.create_tags(Resources=[ami], Tags=[{"Key": KEEP_TAG, "Value": ""}])
    assert _recs(aws) == []


def test_keep_list_is_a_root(aws):
    ec2, _ = aws
    kept, other = _ami(ec2, "kept"), _ami(ec2, "other")
    recs = _recs(aws, tuning=FinOpsTuning(ami_keep=(kept,)))
    assert [r.resource.resource_id for r in recs] == [other]


def test_kept_ami_shields_its_snapshots(aws):
    ec2, _ = aws
    kept, dead = _ami(ec2, "kept"), _ami(ec2, "dead")
    disk = {"DeviceName": "/dev/xvda", "Ebs": {"SnapshotId": "snap-base", "VolumeSize": 50}}
    patch = {kept: {"BlockDeviceMappings": [disk]}, dead: {"BlockDeviceMappings": [disk]}}

    [rec] = _recs(aws, patch=patch, tuning=FinOpsTuning(ami_keep=(kept,)))
    assert rec.resource.resource_id == dead
    assert rec.estimated_savings.amount == 0
    assert "keep snap-base, a live ami uses it" in rec.summary


def test_dlm_tag_on_a_snapshot_is_a_root(aws):
    ec2, _ = aws
    ami = _ami(ec2)
    snap = ec2.describe_images(ImageIds=[ami])["Images"][0]["BlockDeviceMappings"][0]
    ec2.create_tags(
        Resources=[snap["Ebs"]["SnapshotId"]], Tags=[{"Key": DLM_TAG, "Value": "policy-1"}]
    )
    assert _recs(aws) == []


def test_orphan_collapses_its_snapshots_into_one_price(aws):
    ec2, _ = aws
    ami = _ami(ec2)
    disks = [
        {"DeviceName": "/dev/xvda", "Ebs": {"SnapshotId": "snap-root", "VolumeSize": 30}},
        {"DeviceName": "/dev/xvdb", "Ebs": {"SnapshotId": "snap-data", "VolumeSize": 100}},
        {"DeviceName": "/dev/xvdc", "VirtualName": "ephemeral0"},
    ]
    [rec] = _recs(aws, patch={ami: {"BlockDeviceMappings": disks}})

    want = pricing.snapshot_monthly(30, REGION) + pricing.snapshot_monthly(100, REGION)
    assert rec.estimated_savings.amount == want
    assert "2 snapshots (snap-data, snap-root, 130 GiB)" in rec.summary


def test_snapshot_shared_with_a_live_ami_is_not_priced(aws):
    ec2, _ = aws
    live, dead = _ami(ec2, "live"), _ami(ec2, "dead")
    ec2.run_instances(ImageId=live, MinCount=1, MaxCount=1)
    shared = {"DeviceName": "/dev/xvda", "Ebs": {"SnapshotId": "snap-shared", "VolumeSize": 50}}
    own = {"DeviceName": "/dev/xvdb", "Ebs": {"SnapshotId": "snap-own", "VolumeSize": 10}}
    patch = {live: {"BlockDeviceMappings": [shared]}, dead: {"BlockDeviceMappings": [shared, own]}}

    [rec] = _recs(aws, patch=patch)
    assert rec.resource.resource_id == dead
    assert rec.estimated_savings.amount == pricing.snapshot_monthly(10, REGION)
    assert "keep snap-shared" in rec.summary


def test_recent_launch_keeps_it_live(aws):
    ec2, _ = aws
    ami = _ami(ec2)
    assert _recs(aws, patch={ami: {"LastLaunchedTime": _iso(30)}}) == []


def test_too_young_image_is_kept(aws):
    ec2, _ = aws
    ami = _ami(ec2)
    assert _recs(aws, patch={ami: {"CreationDate": _iso(5)}}) == []


def test_thresholds_come_from_tuning(aws):
    ec2, _ = aws
    ami = _ami(ec2)
    patch = {ami: {"CreationDate": _iso(5), "LastLaunchedTime": _iso(10)}}
    tuning = FinOpsTuning(ami_unlaunched_days=7, ami_min_age_days=1)
    assert [r.resource.resource_id for r in _recs(aws, patch=patch, tuning=tuning)] == [ami]


def test_ssm_denied_on_template_versions_makes_everything_live(aws, caplog):
    ec2, _ = aws
    _ami(ec2)
    ec2.create_launch_template(LaunchTemplateName="web", LaunchTemplateData={"ImageId": "ami-x"})
    fail = {"describe_launch_template_versions": _denied(
        "DescribeLaunchTemplateVersions", "not authorized to perform ssm:GetParameters"
    )}
    with caplog.at_level(logging.WARNING):
        assert _recs(aws, fail=fail) == []
    assert "ssm:GetParameters" in caplog.text
    assert "every ami there counts as live" in caplog.text


class _AliasEc2(_Patched):
    """real aws: resolveAlias is refused on a multi-version describe"""

    def __init__(self, client, resolved: str) -> None:
        super().__init__(client)
        self._resolved = resolved

    def get_paginator(self, name: str):
        pag = super().get_paginator(name)
        if name != "describe_launch_template_versions":
            return pag

        class _Strict:
            def paginate(self, **kw):
                if kw.get("ResolveAlias"):
                    raise ClientError(
                        {"Error": {"Code": "InvalidParameterCombination", "Message": "x"}},
                        "DescribeLaunchTemplateVersions",
                    )
                return pag.paginate(**kw)
        return _Strict()

    def describe_launch_template_versions(self, **kw):
        out = self._client.describe_launch_template_versions(**kw)
        assert kw.get("ResolveAlias") and len(kw.get("Versions", [])) == 1
        for v in out["LaunchTemplateVersions"]:
            v["LaunchTemplateData"]["ImageId"] = self._resolved
        return out


def test_ssm_alias_resolved_one_version_at_a_time(aws):
    ec2, asg = aws
    ami, orphan = _ami(ec2), _ami(ec2, "stale")
    ec2.create_launch_template(
        LaunchTemplateName="web", LaunchTemplateData={"ImageId": "resolve:ssm:/golden/latest"}
    )
    ec2.create_launch_template_version(LaunchTemplateName="web", LaunchTemplateData={"ImageId": "ami-y"})
    provider = _Provider(_AliasEc2(ec2, ami), asg)
    recs = ReachabilityCollector(provider, None).recommendations(None)
    assert [r.resource.resource_id for r in recs] == [orphan]


def test_unresolved_ssm_alias_makes_everything_live(aws, caplog):
    ec2, _ = aws
    _ami(ec2)
    ec2.create_launch_template(
        LaunchTemplateName="web", LaunchTemplateData={"ImageId": "resolve:ssm:/golden/latest"}
    )
    with caplog.at_level(logging.WARNING):
        assert _recs(aws) == []
    assert "ssm:GetParameters" in caplog.text


def test_launch_configurations_denied_makes_everything_live(aws, caplog):
    ec2, _ = aws
    _ami(ec2)
    fail = {"describe_launch_configurations": _denied("DescribeLaunchConfigurations")}
    with caplog.at_level(logging.WARNING):
        assert _recs(aws, asg_fail=fail) == []
    assert "autoscaling:DescribeLaunchConfigurations" in caplog.text


def test_image_attribute_denied_makes_everything_live(aws, caplog):
    ec2, asg = aws
    _ami(ec2)

    class _NoAttr(_Patched):
        def describe_image_attribute(self, **kw):
            raise _denied("DescribeImageAttribute")

    provider = _Provider(_NoAttr(ec2), asg)
    with caplog.at_level(logging.WARNING):
        assert ReachabilityCollector(provider).recommendations(None) == []
    assert "ec2:DescribeImageAttribute" in caplog.text


def test_backup_tag_is_a_root(aws):
    ec2, _ = aws
    ami = _ami(ec2)
    ec2.create_tags(Resources=[ami], Tags=[{"Key": BACKUP_TAG, "Value": "arn:aws:ec2:x"}])
    assert _recs(aws) == []


def test_backup_recovery_point_is_a_root(aws):
    ec2, _ = aws
    kept, other = _ami(ec2, "restore-point"), _ami(ec2, "other")
    recs = _recs(aws, backup=_Backup({"Default": [kept]}))
    assert [r.resource.resource_id for r in recs] == [other]
    assert recs[0].approximate is False


def test_backup_denied_never_launched_is_approximate(aws, caplog):
    ec2, _ = aws
    _ami(ec2)
    with caplog.at_level(logging.WARNING):
        [rec] = _recs(aws, backup=_Backup(fail=_denied("ListBackupVaults")))
    assert rec.approximate is True
    assert "check it isn't a backup recovery point" in rec.summary
    assert "backup:ListRecoveryPointsByBackupVault" in caplog.text


def test_backup_denied_launched_stays_exact(aws):
    ec2, _ = aws
    ami = _ami(ec2)
    [rec] = _recs(
        aws, patch={ami: {"LastLaunchedTime": _iso(200)}},
        backup=_Backup(fail=_denied("ListBackupVaults")),
    )
    assert rec.approximate is False
    assert "backup" not in rec.summary
