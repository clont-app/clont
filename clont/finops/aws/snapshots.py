"""FinOps recommendations for stale EBS snapshots — old or orphaned.

Two single-read signals on self-owned snapshots:

* **orphaned** — the source volume no longer exists, so the snapshot is almost
  certainly forgotten,
* **old** — older than a threshold and worth reviewing for a retention policy.

Snapshots are incremental, so the dollar figure (`pricing.snapshot_monthly`) is
an *upper bound* on the source volume's size, not an exact charge. The
placeholder source (``vol-ffffffff``) is never treated as orphaned.

a snapshot that backs an ami is never reported here: behind a live ami it isn't
garbage, behind an orphan ami it is part of that ami's finding in
`reachability`. dlm-owned snapshots are skipped too, the policy deletes them.
only when ``describe_images`` can't be read does the "may back an AMI" caveat
come back.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from clont.core.logging import get_logger
from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.aws import pricing
from clont.finops.aws.reachability import DLM_TAG, read_images
from clont.finops.base import FinOpsTuning
from clont.finops.models import CostRecord, Recommendation
from clont.providers.aws.parsing import _Snapshot
from clont.providers.aws.regions import for_each_region
from clont.providers.base import Provider

log = get_logger("clont.finops.aws.snapshots")

_AMI_PLACEHOLDER = "vol-ffffffff"  # source shown for AMI-backing snapshots
_USD = "USD"


def stale_kind(snap: _Snapshot, live_volumes: set[str], cutoff: datetime) -> str | None:
    """the kind this check reports a snapshot under, None if it doesn't"""
    if (
        snap.volume_id.startswith("vol-")
        and snap.volume_id != _AMI_PLACEHOLDER
        and snap.volume_id not in live_volumes
    ):
        return "orphaned-snapshot"
    if snap.start_time is not None and snap.start_time < cutoff:
        return "old-snapshot"
    return None


@register("finops", Cloud.AWS, "snapshots")
class SnapshotCollector:
    cloud = Cloud.AWS
    service = "snapshots"

    def __init__(self, provider: Provider, tuning: FinOpsTuning | None = None) -> None:
        self._provider = provider
        self._tuning = tuning or FinOpsTuning()

    def collect(self, period: Period) -> list[CostRecord]:
        return []

    def recommendations(self, period: Period) -> list[Recommendation]:
        return for_each_region(self._provider, self._region, what="snapshots")

    def _region(self, region: str) -> list[Recommendation]:
        ec2 = self._provider.client("ec2", region)
        live_volumes = self._live_volume_ids(ec2)
        in_amis = self._ami_snapshot_ids(ec2, region)
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=self._tuning.snapshot_max_age_days)

        out: list[Recommendation] = []
        for page in ec2.get_paginator("describe_snapshots").paginate(OwnerIds=["self"]):
            for raw in page.get("Snapshots", []):
                snap = _Snapshot.model_validate(raw)
                if DLM_TAG in snap.tag_map():
                    continue
                if in_amis is not None and snap.snapshot_id in in_amis:
                    continue
                rec = self._classify(snap, live_volumes, in_amis is not None, cutoff, now, region)
                if rec is not None:
                    out.append(rec)
        return out

    def _live_volume_ids(self, ec2) -> set[str]:
        ids: set[str] = set()
        for page in ec2.get_paginator("describe_volumes").paginate():
            ids.update(v.get("VolumeId", "") for v in page.get("Volumes", []))
        return ids

    def _ami_snapshot_ids(self, ec2, region: str) -> set[str] | None:
        """snapshots any self-owned ami uses, None when images can't be read"""
        try:
            return {s for img in read_images(ec2) for s in img.snapshots()}
        except Exception as exc:  # noqa: BLE001 - fall back to the caveat
            log.warning(
                "snapshots: describe_images failed in %s for %s — add ec2:DescribeImages "
                "to the role; findings keep the ami caveat: %s",
                region, self._provider.alias, exc,
            )
            return None

    def _classify(
        self, snap: _Snapshot, live_volumes: set[str], amis_known: bool,
        cutoff: datetime, now: datetime, region: str,
    ) -> Recommendation | None:
        kind = stale_kind(snap, live_volumes, cutoff)
        size = f"({snap.volume_size} GiB)"
        if kind == "orphaned-snapshot":
            tail = ", no AMI uses it — delete it" if amis_known else " — delete if not backing an AMI"
            return self._rec(
                snap, "orphaned-snapshot", region,
                f"Snapshot of deleted volume {snap.volume_id} {size}{tail}",
            )
        if kind == "old-snapshot" and snap.start_time is not None:
            age_days = (now - snap.start_time).days
            tail = (
                ", no AMI uses it — review retention" if amis_known
                else " — review retention; delete if not backing an AMI"
            )
            return self._rec(snap, "old-snapshot", region, f"Snapshot {age_days}d old {size}{tail}")
        return None

    def _rec(self, snap: _Snapshot, kind: str, region: str, summary: str) -> Recommendation:
        return Recommendation(
            cloud=str(Cloud.AWS),
            service="ebs",
            kind=kind,
            resource=CloudResource(
                cloud=Cloud.AWS,
                service="ebs",
                resource_id=snap.snapshot_id,
                region=region,
                alias=self._provider.alias,
            ),
            summary=summary,
            estimated_savings=Money(
                amount=pricing.snapshot_monthly(snap.volume_size), currency=_USD
            ),
        )
