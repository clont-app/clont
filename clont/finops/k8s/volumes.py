"""Claims nothing mounts, and the namespaces nobody came back for.

The other half of the plan's "two findings only k8s makes visible". A PersistentVolumeClaim
outlives every pod it was made for: delete the Deployment, delete the StatefulSet, and the
pvc stays `Bound`, holding blocks on a datastore the operator's card prices. From the
hypervisor those blocks are invisible — they are not a vm's disk, so the on-prem pass can
only report them as `unaccounted-storage` at the site level, space *something* occupies.
This names them.

    pool card -> $/GiB-month -> the claim no pod mounts

Two kinds, and a namespace gets **one** of them:

| kind | when |
|---|---|
| `abandoned-namespace` | the namespace has no pod at all, live or pending, and still holds claims |
| `unmounted-pvc` | a live namespace holding a bound claim no pod references |

**The rollup replaces the per-claim rows, never adds to them.** An operator deletes the
namespace, not six pvcs one at a time, and pricing both would charge the same GiB twice.
Same rule as one on-prem finding per moref.

Decisions worth keeping:

* **a pending pod counts as life.** A pod stuck unschedulable because this very claim is
  unbound is the strongest possible evidence the namespace is in use — counting only
  scheduled pods would call it abandoned.
* **`$/GiB-month` comes off the pool card** (`rate_storage_gib_month`), averaged over the
  pools this cluster's nodes sit in. With no card — an eks cluster today — the finding is
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
from clont.finops.k8s.mapping import ClusterMapping
from clont.finops.k8s.prices import Prices
from clont.finops.models import CostRecord
from clont.providers.k8s.pods import Pod
from clont.providers.k8s.volumes import Claim

log = get_logger("clont.finops.k8s.volumes")

ABANDONED = "abandoned-namespace"
UNMOUNTED = "unmounted-pvc"

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

    def summary(self) -> str:
        head = (
            f"{self.cluster}: {len(self.findings)} volume finding(s) over {self.claims} "
            f"claim(s), {self.unmounted_gib} GiB nothing mounts"
        )
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
    tuning: FinOpsTuning | None = None,
    now: datetime | None = None,
) -> VolumeReport:
    """Every claim no pod mounts, rolled up per namespace when the namespace is empty too."""
    tune = tuning or FinOpsTuning()
    rate = _gib_month(mapping, records)
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
                _finding(UNMOUNTED, claim.ref, region, [claim], rate, now)
                for claim in old
                if claim.gib >= min_gib
            )
            continue
        if sum((claim.gib for claim in old), Decimal(0)) >= min_gib:
            findings.append(_finding(ABANDONED, namespace, region, old, rate, now))
    report = VolumeReport(
        cluster=mapping.cluster,
        findings=tuple(
            sorted(
                (f for f in findings if f.monthly >= floor or rate <= 0),
                key=lambda f: (-f.monthly, -f.gib, f.ref),
            )
        ),
        claims=len(claims),
        unmounted_gib=_round(unmounted_gib),
        gib_month=rate,
    )
    log.debug("%s", report.summary())
    return report


def _finding(
    kind: str,
    ref: str,
    region: str,
    claims: list[Claim],
    rate: Decimal,
    now: datetime | None,
) -> VolumeFinding:
    gib = sum((claim.gib for claim in claims), Decimal(0))
    return VolumeFinding(
        kind=kind,
        ref=ref,
        region=region,
        summary=_summary(kind, ref, claims, gib, rate, region, now),
        monthly=_money(gib * rate),
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
    priced = (
        # the rate itself is printed finer than cents: 0.292 shown as 0.29 against a
        # 500 GiB claim reads as an arithmetic error
        f"at {_rate(rate)}/GiB-month off {region}'s card that is "
        f"{_money(gib * rate)}/month"
        if rate > 0
        else "the pool publishes no $/GiB-month, so this capacity is unpriced"
    )
    tail = (
        "deleting it deletes the data — a statefulset scaled to zero looks exactly like "
        "this and wants its volumes back. the hypervisor can only see these blocks as "
        "unaccounted datastore space"
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


def _gib_month(mapping: ClusterMapping, records: list[CostRecord]) -> Decimal:
    """The mean $/GiB-month of the pools this cluster sits on, 0 when none publishes one.

    A mean because a claim belongs to a cluster, not to a node: which datastore holds it is
    the next piece of the plan (the pvc -> datastore link), and until then a cluster
    spanning two cards is priced between them rather than at one of their rates.
    """
    prices = Prices(records)
    cards = {}
    for match in mapping.matched:
        card = prices.card(match.target)
        if card is not None and card.storage_gib_month > 0:
            # keyed per pool, not per node: ten nodes on one card is still one rate
            cards[(match.target.alias, match.target.pool)] = card.storage_gib_month
    if not cards:
        return Decimal(0)
    return sum(cards.values(), Decimal(0)) / Decimal(len(cards))


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
