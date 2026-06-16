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
    def __init__(self, snapshots: list[dict], volumes: list[dict]) -> None:
        self._snapshots = snapshots
        self._volumes = volumes

    def get_paginator(self, name: str) -> _Paginator:
        if name == "describe_snapshots":
            return _Paginator([{"Snapshots": self._snapshots}])
        if name == "describe_volumes":
            return _Paginator([{"Volumes": self._volumes}])
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
