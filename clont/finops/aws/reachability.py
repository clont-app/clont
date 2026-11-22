"""reachability gc for the ami chain: mark from the roots, sweep what's left

per region: snapshot -> ami -> instance (any state but terminated), launch
template version, launch configuration. shared, public and dlm-owned amis are
roots, and so is anything the client pins: a `clont:keep` tag on the ami (any
value, clont only reads it) or its id in `finops.ami_keep`. spot / fleet / batch / image builder aren't read, `LastLaunchedTime`
covers any launch and a generous `ami_unlaunched_days` covers rare use.
aws backup recovery points are roots too. that read is optional: denied, and a
never-launched orphan comes back `approximate`, it may be a backup.
an ami id named in any ssm parameter is a root (golden images that terraform or
cloudformation read at deploy time). optional too: denied, and every orphan comes
back `approximate`.
a snapshot of an orphan that is shared, public or named in a launch template's
block device mapping is kept, not put on the delete list.
any other failed read makes every ami in that region live, a false "garbage" is
worse than a miss.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from botocore.exceptions import ClientError

from clont.core.logging import get_logger
from clont.core.models import Cloud, CloudResource, Money, Period
from clont.core.registry import register
from clont.finops.aws import pricing
from clont.finops.base import FinOpsTuning
from clont.finops.models import CostRecord, Recommendation
from clont.providers.aws.parsing import _EIP, _EBSVolume, _Image, _Instance, _NetworkInterface
from clont.providers.aws.regions import for_each_region
from clont.providers.base import Provider

log = get_logger("clont.finops.aws.reachability")

DLM_TAG = "aws:dlm:lifecycle-policy-id"
BACKUP_TAG = "aws:backup:source-resource"
MANAGED_TAGS = (DLM_TAG, BACKUP_TAG)  # a policy or a vault owns these, not the user
_BACKUP_GRANT = "backup:ListBackupVaults and backup:ListRecoveryPointsByBackupVault"
KEEP_TAG = "clont:keep"
_USD = "USD"
_KIND = "orphaned-ami"
_LIVE_STATES = ["pending", "running", "shutting-down", "stopping", "stopped"]
_SSM_ALIAS = "resolve:ssm:"
_SSM_GRANT = "ssm:DescribeParameters and ssm:GetParameters"
_SSM_BATCH = 10  # get_parameters takes at most 10 names
_AMI_ID = re.compile(r"\bami-[0-9a-f]{8,17}\b")
_LIVE_AMI = "a live ami uses it"


@dataclass
class AmiNode:
    image: _Image
    users: list[str] = field(default_factory=list)  # incoming edges
    roots: list[str] = field(default_factory=list)  # why it's a root on its own


@dataclass
class AmiGraph:
    region: str
    amis: dict[str, AmiNode] = field(default_factory=dict)
    images_known: bool = True
    degraded: str = ""  # missing grant, non-empty = every ami here is live
    backup_known: bool = True  # false = a never-launched orphan may be a backup
    ssm_known: bool = True     # false = any orphan may be named in a parameter
    snap_users: dict[str, str] = field(default_factory=dict)  # snapshot -> who uses it directly


@dataclass(frozen=True)
class Orphan:
    image: _Image
    snapshots: dict[str, int]  # the ones it alone holds, id -> GiB
    kept: dict[str, str]       # its snapshots something else still uses -> why
    idle_days: int | None      # since last launch, None = never / pre 2017-04
    unsure: bool = False       # never launched and aws backup unreadable
    ssm_unknown: bool = False  # ssm parameters unreadable


def read_images(ec2) -> list[_Image]:
    """self-owned images, disabled ones too — their snapshots still bill"""
    out: list[_Image] = []
    pages = ec2.get_paginator("describe_images").paginate(
        Owners=["self"], IncludeDisabled=True
    )
    for page in pages:
        out.extend(_Image.model_validate(raw) for raw in page.get("Images", []))
    return out


class _Reads:
    """runs each read, the first failure degrades the whole region"""

    def __init__(self, graph: AmiGraph, alias: str) -> None:
        self._graph = graph
        self._alias = alias

    def run(self, grant: str, fn: Callable[[], None]) -> bool:
        if self._graph.degraded:
            return False
        try:
            fn()
            return True
        except Exception as exc:  # noqa: BLE001 - unknown consumer means live
            self.fail(_grant_for(grant, exc), exc)
            return False

    def fail(self, grant: str, reason: object) -> None:
        self._graph.degraded = grant
        log.warning(
            "reachability: read failed in %s for %s — add %s to the role; "
            "every ami there counts as live: %s",
            self._graph.region, self._alias, grant, reason,
        )


def _grant_for(grant: str, exc: Exception) -> str:
    # resolve:ssm aliases are read through ssm on the caller's behalf
    if "ssm" in str(exc).lower() and "LaunchTemplateVersions" in grant:
        return f"{grant} and ssm:GetParameters"
    return grant


def build_graph(
    provider: Provider, region: str, keep: frozenset[str] = frozenset(),
) -> AmiGraph:
    """every edge into the region's self-owned amis"""
    graph = AmiGraph(region=region)
    reads = _Reads(graph, provider.alias)
    ec2 = provider.client("ec2", region)

    def images() -> None:
        graph.amis = {i.image_id: AmiNode(i) for i in read_images(ec2)}

    if not reads.run("ec2:DescribeImages", images):
        graph.images_known = False
        return graph
    if not graph.amis:
        return graph

    def snap_edges(mappings: list[dict], who: str) -> None:
        for m in mappings or []:
            snap = (m.get("Ebs") or {}).get("SnapshotId", "")
            if snap:
                graph.snap_users.setdefault(snap, who)

    def edge(ami: str, who: str) -> None:
        if ami.startswith(_SSM_ALIAS):
            # alias that didn't resolve, can't tell which ami it means
            reads.fail("ssm:GetParameters", f"{who} uses unresolved {ami}")
        elif ami in graph.amis:
            graph.amis[ami].users.append(who)

    def instances() -> None:
        pages = ec2.get_paginator("describe_instances").paginate(
            Filters=[{"Name": "instance-state-name", "Values": _LIVE_STATES}]
        )
        for page in pages:
            for res in page.get("Reservations", []):
                for raw in res.get("Instances", []):
                    inst = _Instance.model_validate(raw)
                    if inst.state != "terminated":
                        edge(inst.image_id, f"instance {inst.instance_id} ({inst.state})")

    def templates() -> None:
        ids: list[str] = []
        for page in ec2.get_paginator("describe_launch_templates").paginate():
            ids.extend(t["LaunchTemplateId"] for t in page.get("LaunchTemplates", []))
        for lt in ids:
            pages = ec2.get_paginator("describe_launch_template_versions").paginate(
                LaunchTemplateId=lt
            )
            for page in pages:
                for v in page.get("LaunchTemplateVersions", []):
                    data = v.get("LaunchTemplateData", {})
                    ami = data.get("ImageId", "")
                    n = v.get("VersionNumber")
                    snap_edges(data.get("BlockDeviceMappings", []), f"launch template {lt} v{n}")
                    if ami.startswith(_SSM_ALIAS):
                        ami = resolve(lt, n) or ami
                    if ami:
                        edge(ami, f"launch template {lt} v{n}")

    def resolve(lt: str, version: object) -> str:
        # aws resolves an alias only on a single-version describe
        got = ec2.describe_launch_template_versions(
            LaunchTemplateId=lt, Versions=[str(version)], ResolveAlias=True
        ).get("LaunchTemplateVersions", [])
        return got[0].get("LaunchTemplateData", {}).get("ImageId", "") if got else ""

    def configurations() -> None:
        asg = provider.client("autoscaling", region)
        for page in asg.get_paginator("describe_launch_configurations").paginate():
            for lc in page.get("LaunchConfigurations", []):
                name = lc.get("LaunchConfigurationName")
                edge(lc.get("ImageId", ""), f"launch configuration {name}")
                snap_edges(lc.get("BlockDeviceMappings", []), f"launch configuration {name}")

    dlm_snapshots: set[str] = set()

    def dlm() -> None:
        pages = ec2.get_paginator("describe_snapshots").paginate(
            OwnerIds=["self"], Filters=[{"Name": "tag-key", "Values": [DLM_TAG]}]
        )
        for page in pages:
            dlm_snapshots.update(s["SnapshotId"] for s in page.get("Snapshots", []))

    reads.run("ec2:DescribeInstances", instances)
    reads.run("ec2:DescribeLaunchTemplates and ec2:DescribeLaunchTemplateVersions", templates)
    reads.run("autoscaling:DescribeLaunchConfigurations", configurations)
    reads.run("ec2:DescribeSnapshots", dlm)
    backups = _backup_amis(provider, region)
    graph.backup_known = backups is not None
    params = _ssm_amis(provider, region)
    graph.ssm_known = params is not None

    for node in graph.amis.values():
        img = node.image
        if img.public:
            node.roots.append("public")
        if DLM_TAG in img.tag_map() or set(img.snapshots()) & dlm_snapshots:
            node.roots.append("owned by dlm")
        if BACKUP_TAG in img.tag_map():
            node.roots.append("made by aws backup")
        elif backups and img.image_id in backups:
            node.roots.append(f"aws backup recovery point in vault {backups[img.image_id]}")
        if params and img.image_id in params:
            node.roots.append(f"named in ssm parameter {params[img.image_id]}")
        if KEEP_TAG in img.tag_map():
            node.roots.append(f"tagged {KEEP_TAG}")
        if img.image_id in keep:
            node.roots.append("listed in finops.ami_keep")
    return graph


def _backup_amis(provider: Provider, region: str) -> dict[str, str] | None:
    """ami id -> vault for every ec2 recovery point, None = can't read backup"""
    out: dict[str, str] = {}
    try:
        backup = provider.client("backup", region)
        vaults: list[str] = []
        for page in backup.get_paginator("list_backup_vaults").paginate():
            vaults.extend(v["BackupVaultName"] for v in page.get("BackupVaultList", []))
        for vault in vaults:
            pages = backup.get_paginator("list_recovery_points_by_backup_vault").paginate(
                BackupVaultName=vault, ByResourceType="EC2"
            )
            for page in pages:
                for rp in page.get("RecoveryPoints", []):
                    arn = rp.get("RecoveryPointArn", "")
                    if ":image/" in arn:
                        out[arn.rsplit("/", 1)[1]] = vault
    except Exception as exc:  # noqa: BLE001 - optional grant, orphans just get less sure
        log.warning(
            "reachability: aws backup unreadable in %s for %s — add %s to the role; "
            "never-launched amis there come back approximate: %s",
            region, provider.alias, _BACKUP_GRANT, exc,
        )
        return None
    return out


def _ssm_amis(provider: Provider, region: str) -> dict[str, str] | None:
    """ami id -> the ssm parameter naming it, None = can't read ssm"""
    out: dict[str, str] = {}
    try:
        ssm = provider.client("ssm", region)
        names: list[str] = []
        for page in ssm.get_paginator("describe_parameters").paginate():
            # securestring is a secret, never an ami pointer worth decrypting
            names.extend(
                p["Name"] for p in page.get("Parameters", []) if p.get("Type") != "SecureString"
            )
        for i in range(0, len(names), _SSM_BATCH):
            got = ssm.get_parameters(Names=names[i : i + _SSM_BATCH])
            for p in got.get("Parameters", []):
                for ami in _AMI_ID.findall(p.get("Value", "")):
                    out.setdefault(ami, p["Name"])
    except Exception as exc:  # noqa: BLE001 - optional grant, orphans just get less sure
        log.warning(
            "reachability: ssm parameters unreadable in %s for %s — add %s to the role; "
            "orphan amis there come back approximate: %s",
            region, provider.alias, _SSM_GRANT, exc,
        )
        return None
    return out


def _snapshot_shared(ec2, snap: str) -> str:
    """why a snapshot can't go, "" if it can"""
    try:
        attr = ec2.describe_snapshot_attribute(SnapshotId=snap, Attribute="createVolumePermission")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "InvalidSnapshot.NotFound":
            return ""
        log.warning("reachability: can't read sharing of %s, keeping it: %s", snap, exc)
        return "sharing unreadable (ec2:DescribeSnapshotAttribute)"
    except Exception as exc:  # noqa: BLE001 - unknown means keep
        log.warning("reachability: can't read sharing of %s, keeping it: %s", snap, exc)
        return "sharing unreadable (ec2:DescribeSnapshotAttribute)"
    perms = attr.get("CreateVolumePermissions", [])
    if any(p.get("Group") == "all" for p in perms):
        return "it is public"
    return "it is shared with another account" if perms else ""


def volume_users(vol: _EBSVolume) -> list[str]:
    """edges into a volume, one per attachment still holding it"""
    return [
        f"instance {a.get('InstanceId', '?')} ({a.get('State', '')})"
        for a in vol.attachments if a.get("State") != "detached"
    ]


def eni_users(eni: _NetworkInterface) -> list[str]:
    """edge into an eni: whatever its attachment points at, instance or service"""
    if not eni.attachment_id or eni.attachment_status == "detached":
        return []
    return [f"attachment {eni.attachment_id} ({eni.attachment_status})"]


def orphan_volumes(ec2) -> list[_EBSVolume]:
    """volumes nothing reaches. parity self-test: must equal waste's unattached-ebs"""
    out: list[_EBSVolume] = []
    for page in ec2.get_paginator("describe_volumes").paginate():
        for raw in page.get("Volumes", []):
            vol = _EBSVolume.model_validate(raw)
            if not volume_users(vol):
                out.append(vol)
    return out


def orphan_enis(ec2) -> list[_NetworkInterface]:
    """enis nothing reaches that hold a billed address.
    parity self-test: must equal public_ipv4's unattached-eni-ip"""
    byoip = {
        e.public_ip for raw in ec2.describe_addresses().get("Addresses", [])
        if (e := _EIP.model_validate(raw)).byoip and e.public_ip
    }
    out: list[_NetworkInterface] = []
    for page in ec2.get_paginator("describe_network_interfaces").paginate():
        for raw in page.get("NetworkInterfaces", []):
            eni = _NetworkInterface.model_validate(raw)
            if not eni_users(eni) and set(eni.public_ips()) - byoip:
                out.append(eni)
    return out


def find_orphans(
    provider: Provider, graph: AmiGraph, tuning: FinOpsTuning, now: datetime,
) -> list[Orphan]:
    """amis nothing reaches, each with the snapshots only it holds"""
    if graph.degraded or not graph.images_known:
        return []
    young = now - timedelta(days=tuning.ami_min_age_days)
    launched = now - timedelta(days=tuning.ami_unlaunched_days)

    candidates: list[AmiNode] = []
    for node in graph.amis.values():
        img = node.image
        if node.users or node.roots or img.state != "available":
            continue
        if img.created is None or img.created > young:
            continue
        if img.last_launched is not None and img.last_launched > launched:
            continue
        candidates.append(node)

    ec2 = provider.client("ec2", graph.region)
    reads = _Reads(graph, provider.alias)
    for node in candidates:
        def shared(node: AmiNode = node) -> None:
            attr = ec2.describe_image_attribute(
                ImageId=node.image.image_id, Attribute="launchPermission"
            )
            if attr.get("LaunchPermissions"):
                node.roots.append("shared")
        if not reads.run("ec2:DescribeImageAttribute", shared):
            return []

    orphan_ids = {n.image.image_id for n in candidates if not n.roots}
    live_snaps = {
        s for a, n in graph.amis.items() if a not in orphan_ids for s in n.image.snapshots()
    }
    claimed: set[str] = set()
    out: list[Orphan] = []
    for ami in sorted(orphan_ids):
        img = graph.amis[ami].image
        kept: dict[str, str] = {}
        own: dict[str, int] = {}
        for s, gib in img.snapshots().items():
            if s in claimed:
                continue
            if s in live_snaps:
                kept[s] = _LIVE_AMI
            elif s in graph.snap_users:
                kept[s] = f"{graph.snap_users[s]} uses it"
            elif why := _snapshot_shared(ec2, s):
                kept[s] = why
            else:
                own[s] = gib
        claimed.update(own)
        idle = None if img.last_launched is None else (now - img.last_launched).days
        unsure = idle is None and not graph.backup_known
        out.append(Orphan(img, own, kept, idle, unsure, not graph.ssm_known))
    return out


@register("finops", Cloud.AWS, "reachability")
class ReachabilityCollector:
    cloud = Cloud.AWS
    service = "reachability"

    def __init__(self, provider: Provider, tuning: FinOpsTuning | None = None) -> None:
        self._provider = provider
        self._tuning = tuning or FinOpsTuning()

    def collect(self, period: Period) -> list[CostRecord]:
        return []

    def recommendations(self, period: Period) -> list[Recommendation]:
        return for_each_region(self._provider, self._region, what="ami reachability")

    def _region(self, region: str) -> list[Recommendation]:
        graph = build_graph(self._provider, region, frozenset(self._tuning.ami_keep))
        orphans = find_orphans(self._provider, graph, self._tuning, datetime.now(UTC))
        return [self._rec(o, region) for o in orphans]

    def _rec(self, orphan: Orphan, region: str) -> Recommendation:
        quotes = [pricing.snapshot_quote(gib, region) for gib in orphan.snapshots.values()]
        amount = sum((q.amount for q in quotes), Decimal(0))
        return Recommendation(
            cloud=str(Cloud.AWS),
            service="ec2",
            kind=_KIND,
            resource=CloudResource(
                cloud=Cloud.AWS,
                service="ec2",
                resource_id=orphan.image.image_id,
                region=region,
                alias=self._provider.alias,
            ),
            summary=_summary(orphan),
            estimated_savings=Money(amount=amount, currency=_USD),
            priced_region=quotes[0].region if quotes else region,
            approximate=orphan.unsure or orphan.ssm_unknown or any(q.approximate for q in quotes),
        )


def _summary(orphan: Orphan) -> str:
    img = orphan.image
    name = f" ({img.name})" if img.name else ""
    if orphan.idle_days is None:
        launch = "never launched (or not since 2017-04, when aws began tracking it)"
    else:
        launch = f"last launched {orphan.idle_days}d ago"
    path = (
        f"AMI {img.image_id}{name} is unreachable: no instance, no launch template "
        f"version, no launch configuration uses it; {launch}"
    )
    snaps = orphan.snapshots
    if snaps:
        what = _plural(len(snaps), "snapshot")
        action = (
            f" — deregister it and delete its {what} "
            f"({', '.join(sorted(snaps))}, {sum(snaps.values())} GiB)"
        )
    else:
        action = " — deregister it"
    by_why: dict[str, list[str]] = {}
    for snap, why in sorted(orphan.kept.items()):
        by_why.setdefault(why, []).append(snap)
    for why, ids in by_why.items():
        action += f"; keep {', '.join(ids)}, {why}"
    if orphan.unsure:
        action += (
            f"; aws backup wasn't readable ({_BACKUP_GRANT}), "
            "check it isn't a backup recovery point first"
        )
    if orphan.ssm_unknown:
        action += (
            f"; ssm parameters weren't readable ({_SSM_GRANT}), "
            "check no parameter names it first"
        )
    return path + action


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"
