"""Stale EBS snapshot detection: orphaned (source gone) + old."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from clont.events.detectors import RecommendationDetector
from clont.finops.aws.snapshots import SnapshotCollector


class _Paginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **kw):
        yield from self._pages


class _FakeEC2:
    def __init__(
        self, snapshots: list[dict], volumes: list[dict], images: list[dict] | None = None,
    ) -> None:
        self._snapshots = snapshots
        self._volumes = volumes
        self._images = images or []

    def get_paginator(self, name: str) -> _Paginator:
        if name == "describe_snapshots":
            return _Paginator([{"Snapshots": self._snapshots}])
        if name == "describe_volumes":
            return _Paginator([{"Volumes": self._volumes}])
        if name == "describe_images":
            return _Paginator([{"Images": self._images}])
        raise AssertionError(name)


class _FakeProvider:
    def __init__(self, ec2: _FakeEC2, alias: str = "prod") -> None:
        self._ec2 = ec2
        self.alias = alias

    def regions(self) -> list[str]:
        return ["us-east-1"]

    def client(self, service: str, region: str | None = None):
        assert service == "ec2"
        return self._ec2


def _snap(snap_id: str, volume_id: str, size: int, age_days: int) -> dict:
    return {
        "SnapshotId": snap_id,
        "VolumeId": volume_id,
        "VolumeSize": size,
        "StartTime": datetime.now(UTC) - timedelta(days=age_days),
    }


def test_orphaned_and_old_snapshots_flagged_recent_ones_ignored():
    snaps = [
        _snap("snap-orphan", "vol-gone", 100, age_days=10),   # source deleted -> orphaned
        _snap("snap-old", "vol-live", 50, age_days=200),      # source live but very old
        _snap("snap-recent", "vol-live", 8, age_days=5),      # live + recent -> ignore
        _snap("snap-ami", "vol-ffffffff", 8, age_days=300),   # AMI placeholder -> not orphaned, but old
    ]
    volumes = [{"VolumeId": "vol-live"}]
    recs = SnapshotCollector(_FakeProvider(_FakeEC2(snaps, volumes))).recommendations(None)

    by_id = {r.resource.resource_id: r for r in recs}
    assert by_id["snap-orphan"].kind == "orphaned-snapshot"
    assert by_id["snap-orphan"].estimated_savings.amount == Decimal("5.00")  # 100 * 0.05
    assert by_id["snap-old"].kind == "old-snapshot"
    assert by_id["snap-ami"].kind == "old-snapshot"     # placeholder source -> old, not orphaned
    assert "snap-recent" not in by_id


def test_snapshot_event_key_includes_kind():
    snaps = [_snap("snap-orphan", "vol-gone", 10, age_days=5)]
    recs = SnapshotCollector(_FakeProvider(_FakeEC2(snaps, []))).recommendations(None)
    [event] = RecommendationDetector().detect(recs)
    assert event.key == "finops:rec:prod:aws:ebs:orphaned-snapshot:snap-orphan"


def test_no_snapshots_no_recs():
    recs = SnapshotCollector(_FakeProvider(_FakeEC2([], []))).recommendations(None)
    assert recs == []


def _image(ami: str, *snaps: str) -> dict:
    return {
        "ImageId": ami,
        "BlockDeviceMappings": [{"Ebs": {"SnapshotId": s, "VolumeSize": 8}} for s in snaps],
    }


def test_snapshot_backing_an_ami_is_left_to_reachability():
    snaps = [
        _snap("snap-in-ami", "vol-gone", 8, age_days=400),
        _snap("snap-loose", "vol-gone", 8, age_days=400),
    ]
    ec2 = _FakeEC2(snaps, [], images=[_image("ami-1", "snap-in-ami")])
    recs = SnapshotCollector(_FakeProvider(ec2)).recommendations(None)

    [rec] = recs
    assert rec.resource.resource_id == "snap-loose"
    assert rec.kind == "orphaned-snapshot"
    assert "no AMI uses it" in rec.summary
    assert "if not backing" not in rec.summary


def test_dlm_snapshot_is_not_reported():
    snap = _snap("snap-dlm", "vol-gone", 8, age_days=400)
    snap["Tags"] = [{"Key": "aws:dlm:lifecycle-policy-id", "Value": "policy-1"}]
    recs = SnapshotCollector(_FakeProvider(_FakeEC2([snap], []))).recommendations(None)
    assert recs == []


def test_backup_snapshot_is_not_reported():
    # source volume gone is normal for a backup, deleting it kills the restore
    snap = _snap("snap-bk", "vol-gone", 8, age_days=400)
    snap["Tags"] = [{"Key": "aws:backup:source-resource", "Value": "arn:aws:ec2:x:1:volume/vol-gone"}]
    recs = SnapshotCollector(_FakeProvider(_FakeEC2([snap], []))).recommendations(None)
    assert recs == []


class _NoImagesEC2(_FakeEC2):
    def get_paginator(self, name: str) -> _Paginator:
        if name == "describe_images":
            raise RuntimeError("AccessDenied")
        return super().get_paginator(name)


def test_unreadable_images_keep_the_ami_caveat():
    snaps = [_snap("snap-orphan", "vol-gone", 8, age_days=5)]
    [rec] = SnapshotCollector(_FakeProvider(_NoImagesEC2(snaps, []))).recommendations(None)
    assert rec.kind == "orphaned-snapshot"
    assert "delete if not backing an AMI" in rec.summary
