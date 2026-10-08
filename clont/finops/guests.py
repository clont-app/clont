"""What a guest platform admits to holding on a site's datastores.

One tiny contract between two halves that must not import each other: the kubernetes
source fills it, the on-prem waste pass subtracts it. Without it the hypervisor sees
datastore space no vm accounts for and calls it isos and dead vm folders, while the k8s
sweep prices the very same blocks as `unmounted-pvc` — the same money, twice, in one
report. With it the gap says how much of itself is somebody's volume, and who to ask.

**Only what no vm already carries belongs in `detached_gib`.** A block volume attached to
a node vm is inside that vm's committed storage, so it is on the invoice once already;
subtracting it would shrink a gap it is not in. Everything else is carried for the log
line rather than the arithmetic, because the interesting number is always the one that was
*not* subtracted.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class GuestStorage:
    """Blocks on one site's datastores that belong to a guest platform, not to a vm."""

    source: str                           # what reported it — a cluster name
    detached_gib: Decimal = Decimal(0)    # no vm holds these, so they *are* the site's gap
    volumes: int = 0                      # how many volumes that is
    attached_gib: Decimal = Decimal(0)    # a node vm has them, so already inside its disk
    in_node_gib: Decimal = Decimal(0)     # inside a node vm's disk, already in its committed
    unplaced_gib: Decimal = Decimal(0)    # of the detached, what named no datastore
    unknown_gib: Decimal = Decimal(0)     # a driver with no rule: deliberately not subtracted
    elsewhere_gib: Decimal = Decimal(0)   # nfs, ceph, a cloud disk, another site's array

    def line(self) -> str:
        """One line for the log: what was reconciled, and what was left out of it."""
        out = (
            f"{self.source}: {self.volumes} volume(s), {_gib(self.detached_gib)} GiB on "
            f"datastores no vm holds"
        )
        extra = [
            (self.unplaced_gib, "on no named datastore"),
            (self.attached_gib, "attached to node vms"),
            (self.in_node_gib, "inside node disks"),
            (self.unknown_gib, "on an unknown driver"),
            (self.elsewhere_gib, "not on this site's datastores"),
        ]
        said = ", ".join(f"{_gib(gib)} GiB {what}" for gib, what in extra if gib > 0)
        return f"{out} ({said})" if said else out


def _gib(value: Decimal) -> str:
    return f"{float(value):,.0f}"
