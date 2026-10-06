"""PersistentVolumeClaims, turned into the capacity somebody is still paying for.

Same split as `nodes.py` / `pods.py`: `client.py` does the read, nothing here knows a
kubernetes type. A claim is the only piece of a cluster that **outlives every pod it was
made for** — delete the Deployment and the pvc stays, bound, holding blocks on a datastore
the operator's card already prices. That is why it is read at all.

Three things decided here:

* **the size is `status.capacity`, not the request.** The request is what was asked for;
  the capacity is what the provisioner actually cut, and a storage class with a minimum
  volume size hands out more than was asked. A claim with no status capacity falls back to
  its request, because a Pending claim has never been given anything.
* **only a `Bound` claim holds iron.** A Pending one is waiting for a provisioner — the
  blocks do not exist yet, so pricing it would invoice an intention. It is still carried,
  with its phase, so a report can say the cluster is waiting on storage.
* **the age comes off `creationTimestamp` and is kept as a date, not a judgement.** A pvc
  made an hour ago is almost always mid-deploy; the threshold that calls it abandoned is
  the finops half's business, not the reader's.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from clont.providers.k8s.nodes import BYTES_PER_GIB, quantity

BOUND = "Bound"


@dataclass(frozen=True, slots=True)
class Claim:
    """One pvc: where it is, how big, and what it is attached to."""

    namespace: str
    name: str
    gib: Decimal = Decimal(0)
    phase: str = ""
    storage_class: str = ""
    volume: str = ""                  # the bound PersistentVolume, the join for the datastore link
    created: datetime | None = None   # None when the api did not say, i.e. age unknown

    @property
    def ref(self) -> str:
        """How an operator finds it: `namespace/name`."""
        return f"{self.namespace}/{self.name}"

    @property
    def bound(self) -> bool:
        return self.phase == BOUND

    def age_days(self, now: datetime | None = None) -> Decimal | None:
        """Days since it was created, or None when the claim carries no timestamp."""
        if self.created is None:
            return None
        moment = now or datetime.now(timezone.utc)
        return Decimal(str((moment - self.created).total_seconds() / 86400))


def build_claims(items: Iterable[dict]) -> list[Claim]:
    """`list_persistent_volume_claim_for_all_namespaces().items` as clont sees it."""
    return [_claim(item) for item in items if isinstance(item, dict)]


def _claim(item: dict) -> Claim:
    meta = _sub(item, "metadata")
    spec = _sub(item, "spec")
    status = _sub(item, "status")
    given = quantity(_sub(status, "capacity").get("storage"))
    asked = quantity(_sub(_sub(spec, "resources"), "requests").get("storage"))
    return Claim(
        namespace=_text(meta.get("namespace")),
        name=_text(meta.get("name")),
        gib=(given or asked) / BYTES_PER_GIB,
        phase=_text(status.get("phase")),
        storage_class=_text(spec.get("storageClassName")),
        volume=_text(spec.get("volumeName")),
        created=_stamp(meta.get("creationTimestamp")),
    )


def _stamp(value: object) -> datetime | None:
    """An api timestamp (`2026-10-06T11:12:13Z`) as an aware datetime, or None."""
    text = _text(value)
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _sub(item: dict, key: str) -> dict:
    value = item.get(key)
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()
