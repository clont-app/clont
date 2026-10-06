"""Claims nothing mounts, and the namespaces nobody came back for.

The other half of the plan's "two findings only k8s makes visible". A PersistentVolumeClaim
outlives every pod it was made for: delete the Deployment, delete the StatefulSet, and the
pvc stays `Bound`, holding blocks on a datastore the operator's card prices. From the
hypervisor those blocks are invisible — they are not a vm's disk, so the on-prem pass can
only report them as `unaccounted-storage` at the site level, space *something* occupies.
This names them.

    pool card -> $/GiB-month -> the claim no pod mounts

Three kinds; a namespace gets **one** of the first two:

| kind | when |
|---|---|
| `abandoned-namespace` | the namespace has no pod at all, live or pending, and still holds claims |
| `unmounted-pvc` | a live namespace holding a bound claim no pod references |
| `released-pv` | the claim is already deleted and `reclaimPolicy: Retain` kept the volume |

**The rollup replaces the per-claim rows, never adds to them.** An operator deletes the
namespace, not six pvcs one at a time, and pricing both would charge the same GiB twice.
Same rule as one on-prem finding per moref.

Decisions worth keeping:

* **a pending pod counts as life.** A pod stuck unschedulable because this very claim is
  unbound is the strongest possible evidence the namespace is in use — counting only
  scheduled pods would call it abandoned.
* **`$/GiB-month` comes off the pool card** (`rate_storage_gib_month`) of the pool whose
  datastore actually holds the volume, and off the mean of the cluster's pools only when
  the volume names no datastore. With no card — an eks cluster today — the finding is
  still emitted at `$0.00` and says the capacity is unpriced: a 500 GiB claim nothing
  mounts is worth reporting without a price on it.
* **only `Bound` claims are priced.** A Pending claim has no blocks yet, so a price would
  be invoicing an intention.
* **young claims are left alone.** A pvc created minutes ago with no pod on it is a deploy
  in progress, not waste. A claim whose timestamp the api did not give passes the gate and
  says the age is unknown rather than being silently dropped.
* **deleting a claim deletes data, and the summary says so.** A StatefulSet scaled to zero
  looks exactly like an abandoned one and wants its volumes back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from clont.core.logging import get_logger
from clont.finops.base import FinOpsTuning
from clont.finops.k8s.datastores import Placements
from clont.finops.k8s.mapping import ClusterMapping
from clont.finops.k8s.prices import Prices
from clont.finops.models import CostRecord
from clont.providers.k8s.pods import Pod
from clont.providers.k8s.volumes import Claim

log = get_logger("clont.finops.k8s.volumes")

ABANDONED = "abandoned-namespace"
UNMOUNTED = "unmounted-pvc"
RELEASED = "released-pv"

_CENT = Decimal("0.01")
_USD = "USD"


@dataclass(frozen=True, slots=True)
class VolumeFinding:
    """One thing to reclaim: a namespace with nothing running in it, or a lone claim."""

    kind: str
    ref: str          # `namespace` for the rollup, `namespace/name` for a claim
    region: str
    summary: str
    monthly: Decimal
    currency: str = _USD
    gib: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class VolumeReport:
    """One cluster's claims: what nothing mounts, and whether it could be priced."""

    cluster: str
    findings: tuple[VolumeFinding, ...] = ()
    claims: int = 0
    unmounted_gib: Decimal = Decimal(0)
    gib_month: Decimal = Decimal(0)   # 0 = no pool card, so the findings carry no price
    released: int = 0                 # pvs whose claim is gone and whose blocks are not

    def summary(self) -> str:
        head = (
            f"{self.cluster}: {len(self.findings)} volume finding(s) over {self.claims} "
            f"claim(s), {self.unmounted_gib} GiB nothing mounts"
        )
        if self.released:
            head += f", {self.released} released pv(s)"
        if self.gib_month <= 0:
            head += " — unpriced: no pool card to read a $/GiB-month off"
        return head


def reclaim(
    mapping: ClusterMapping,
    pods: list[Pod],
    pending: list[Pod],
    claims: list[Claim],
    records: list[CostRecord],
    *,
    placements: Placements | None = None,
    tuning: FinOpsTuning | None = None,
    now: datetime | None = None,
) -> VolumeReport:
    """Every claim no pod mounts, rolled up per namespace when the namespace is empty too."""
    tune = tuning or FinOpsTuning()
    rates = _Rates(mapping, records)
    placed = placements or Placements(cluster=mapping.cluster)
    region = ", ".join(mapping.pools) or "unmapped"
    mounted = {(pod.namespace, claim) for pod in [*pods, *pending] for claim in pod.claims}
    alive = {pod.namespace for pod in [*pods, *pending]}
    min_gib = Decimal(str(tune.k8s_claim_min_gib))
    min_age = Decimal(str(tune.k8s_claim_min_age_days))
    floor = Decimal(str(tune.onprem_min_savings_usd))

    findings: list[VolumeFinding] = []
    unmounted_gib = Decimal(0)
    for namespace, rows in _by_namespace(claims).items():
        idle = [
            claim
            for claim in rows
            if claim.bound and (claim.namespace, claim.name) not in mounted
        ]
        unmounted_gib += sum((claim.gib for claim in idle), Decimal(0))
        old = [claim for claim in idle if _old_enough(claim, min_age, now)]
        if not old:
            continue
        if namespace in alive:
            findings.extend(
                _finding(UNMOUNTED, claim.ref, region, [claim], rates, placed, now)
                for claim in old
                if claim.gib >= min_gib
            )
            continue
        if sum((claim.gib for claim in old), Decimal(0)) >= min_gib:
            findings.append(_finding(ABANDONED, namespace, region, old, rates, placed, now))
    findings.extend(_released(placed, rates, region, min_gib, min_age, now))
    report = VolumeReport(
        cluster=mapping.cluster,
        findings=tuple(
            sorted(
                (f for f in findings if f.monthly >= floor or rates.mean <= 0),
                key=lambda f: (-f.monthly, -f.gib, f.ref),
            )
        ),
        claims=len(claims),
        unmounted_gib=_round(unmounted_gib),
        gib_month=rates.mean,
        released=len(placed.released),
    )
    log.debug("%s", report.summary())
    return report


def _released(
    placed: Placements,
    rates: _Rates,
    region: str,
    min_gib: Decimal,
    min_age: Decimal,
    now: datetime | None,
) -> list[VolumeFinding]:
    """Volumes whose claim is already gone — the shape no pvc sweep can find.

    `reclaimPolicy: Retain` is why they are still here: deleting the pvc released the
    volume and kept the data. There is nothing left pointing at them in the cluster, and
    from the hypervisor they are datastore space no vm holds, so this finding is the only
    place they are ever named.
    """
    out: list[VolumeFinding] = []
    for volume in placed.released:
        if volume.gib < min_gib:
            continue
        age = volume.age_days(now)
        if age is not None and age < min_age:
            continue
        pool = placed.by_claim.get(volume.claim)
        rate = rates.of(pool.pool if pool is not None else "")
        monthly = _money(volume.gib * rate)
        where = f" on {pool.datastore}" if pool is not None and pool.datastore else ""
        was = f" of the deleted claim {volume.claim}" if volume.claim else ""
        policy = f" ({volume.reclaim or 'Retain'})"
        out.append(
            VolumeFinding(
                kind=RELEASED,
                ref=volume.name,
                region=region,
                summary=(
                    f"{volume.name} is Released{where}: {_round(volume.gib)} GiB{was} that "
                    f"the reclaim policy{policy} kept. no claim, no pod and no vm holds it, "
                    f"so nothing else in either half of clont can see it — "
                    f"{_priced(volume.gib, rate, region)}. the data is still on the array: "
                    "delete the pv when it is not the restore you are keeping"
                ),
                monthly=monthly,
                gib=_round(volume.gib),
            )
        )
    return out


def _finding(
    kind: str,
    ref: str,
    region: str,
    claims: list[Claim],
    rates: _Rates,
    placed: Placements,
    now: datetime | None,
) -> VolumeFinding:
    gib = sum((claim.gib for claim in claims), Decimal(0))
    # each claim at the rate of the pool whose datastore actually holds it, so a namespace
    # spanning two arrays is not priced at one of their rates
    monthly = sum(
        (claim.gib * rates.of(placed.pool_of(claim)) for claim in claims), Decimal(0)
    )
    rate = monthly / gib if gib > 0 else Decimal(0)
    return VolumeFinding(
        kind=kind,
        ref=ref,
        region=region,
        summary=_summary(kind, ref, claims, gib, rate, region, now),
        monthly=_money(monthly),
        gib=_round(gib),
    )


def _summary(
    kind: str,
    ref: str,
    claims: list[Claim],
    gib: Decimal,
    rate: Decimal,
    region: str,
    now: datetime | None,
) -> str:
    age = _age_phrase(claims, now)
    size = f"{_round(gib)} GiB"
    priced = _priced(gib, rate, region)
    tail = (
        "deleting it deletes the data — a statefulset scaled to zero looks exactly like "
        "this and wants its volumes back. the hypervisor can only see these blocks as "
        "unaccounted datastore space, so the site's gap hands them here rather than "
        "offering the same GiB back twice"
    )
    if kind == ABANDONED:
        names = ", ".join(claim.name for claim in claims)
        return (
            f"namespace {ref} runs no pod at all, not even a pending one, and still holds "
            f"{len(claims)} bound claim(s) ({names}) of {size} {age} — {priced}. a "
            f"namespace that only runs cronjobs looks like this between runs, so check "
            f"before reclaiming: {tail}"
        )
    claim = claims[0]
    where = f" on {claim.storage_class}" if claim.storage_class else ""
    return (
        f"{ref} is bound to {size}{where} and no pod in the namespace mounts it {age} — "
        f"{priced}. {tail}"
    )


def _priced(gib: Decimal, rate: Decimal, region: str) -> str:
    """What the capacity costs, or why it has no price."""
    if rate <= 0:
        return "the pool publishes no $/GiB-month, so this capacity is unpriced"
    # the rate itself is printed finer than cents: 0.292 shown as 0.29 against a 500 GiB
    # claim reads as an arithmetic error
    return (
        f"at {_rate(rate)}/GiB-month off {region}'s card that is {_money(gib * rate)}/month"
    )


def _age_phrase(claims: list[Claim], now: datetime | None) -> str:
    ages = [claim.age_days(now) for claim in claims]
    known = [age for age in ages if age is not None]
    if not known:
        return "(age unknown: the api gave no creation timestamp)"
    return f"({min(known).quantize(Decimal(1), rounding=ROUND_HALF_UP)}+ days old)"


def _old_enough(claim: Claim, min_age: Decimal, now: datetime | None) -> bool:
    """An unknown age passes: the claim is real, and the threshold simply cannot be applied."""
    age = claim.age_days(now)
    return age is None or age >= min_age


class _Rates:
    """The $/GiB-month a claim is priced at: its own pool's when known, the mean otherwise.

    The pool comes from the datastore the volume actually sits on (`datastores.py`), which
    is the whole point of the pvc -> datastore link: a cluster whose nodes span two cards
    held its volumes on *one* of the two arrays, and the mean was never the right rate for
    either. The mean stays as the fallback, for a volume that names no datastore and for a
    claim read without its pv.
    """

    def __init__(self, mapping: ClusterMapping, records: list[CostRecord]) -> None:
        prices = Prices(records)
        self._by_pool: dict[str, Decimal] = {}
        for match in mapping.matched:
            card = prices.card(match.target)
            if card is not None and card.storage_gib_month > 0:
                # keyed per pool, not per node: ten nodes on one card is still one rate
                self._by_pool[match.target.pool] = card.storage_gib_month
        self.mean = (
            sum(self._by_pool.values(), Decimal(0)) / Decimal(len(self._by_pool))
            if self._by_pool
            else Decimal(0)
        )

    def of(self, pool: str) -> Decimal:
        """The rate for one pool key, falling back to the cluster's mean."""
        return self._by_pool.get(pool) or self.mean


def _by_namespace(claims: list[Claim]) -> dict[str, list[Claim]]:
    out: dict[str, list[Claim]] = {}
    for claim in claims:
        out.setdefault(claim.namespace, []).append(claim)
    return out


def _money(amount: Decimal) -> Decimal:
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


def _rate(rate: Decimal) -> Decimal:
    return rate.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _round(gib: Decimal) -> Decimal:
    return gib.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
