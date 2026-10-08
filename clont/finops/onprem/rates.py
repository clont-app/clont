"""Rate-card allocation: the operator's pool cost over the capacity we measured.

A datacenter sends no invoice, so clont never guesses a price. The operator gives the
monthly cost of one pool, the collector measures that pool's capacity, and the division
is the whole product:

    pool_monthly        = sum of the rate-card lines
    rate_vcpu_hour      = pool_monthly * weights.cpu     / (vcpu * hours)
    rate_ram_gib_hour   = pool_monthly * weights.ram     / (ram_gib * hours)
    rate_storage_gib_mo = pool_monthly * weights.storage / storage_gib

Each vm is then charged twice — on what it reserved and on what it actually used — and
the gap between the two is the waste report.

One pool per call, and a pool is a *cluster* (or a standalone host), never a site:
different clusters are different hardware generations and different licensing, so
averaging them hides the comparison the report exists for. Merging a site card into a
cluster card happens in config, line by line; by the time it arrives here it is one
flat card.

Three things this module refuses to do, because each turns a missing measurement into a
number that reads as measured:

* a zero in `capacity` raises — that is a failed inventory pass, not a free datacenter
* an empty (or all-zero) rate card raises — a $0.00 report is worse than no report
* an unrecognised rate-card line or weight raises instead of being dropped — a typo
  would quietly lower the pool, and every number below it

Overcommit and headroom are *reported*, never clamped: 1.5x oversubscribed charges 1.5x
the pool and headroom goes negative, which is the single number a capacity conversation
needs. Money comes back unrounded; quantizing is the report's job.
"""

from __future__ import annotations

from decimal import Decimal

from clont.core.errors import ConfigError

HOURS_PER_MONTH = 730
DEFAULT_WEIGHTS = {"cpu": 0.5, "ram": 0.3, "storage": 0.2}

# what an operator may put on a card. hardware is either an amortization line or
# capex over a lifetime
COST_LINES = (
    "hardware_amortization",
    "power_and_cooling",
    "rack_and_network",
    "licenses",
    "support",
    "staff",
)
CAPEX_LINES = ("hardware_capex", "lifetime_months")

_CAPACITY = ("vcpu", "ram_gib", "storage_gib")
_WEIGHTS = ("cpu", "ram", "storage")
_USAGE = ("vcpu", "ram_gib", "disk_gib")

_WEIGHT_SUM_TOLERANCE = Decimal("0.000001")


def allocate(payload: dict) -> dict:
    """Charge one pool's vms against its rate card.

    in : {hours_per_month, weights, rate_card, capacity, vms}
    out: {pool_monthly, rates, vms: {name: {provisioned, used, waste}},
          total_provisioned, total_used, headroom, allocated_ratio, overcommit}
    """
    hours = _positive(payload.get("hours_per_month", HOURS_PER_MONTH), "hours_per_month")
    weights = _weights(payload.get("weights"))
    capacity = _capacity(payload.get("capacity") or {})
    pool = pool_monthly(payload.get("rate_card") or {})

    rates = {
        "vcpu_hour": pool * weights["cpu"] / (capacity["vcpu"] * hours),
        "ram_gib_hour": pool * weights["ram"] / (capacity["ram_gib"] * hours),
        "storage_gib_month": pool * weights["storage"] / capacity["storage_gib"],
    }

    vms: dict[str, dict[str, float]] = {}
    reserved = dict.fromkeys(_USAGE, Decimal(0))
    total_provisioned = Decimal(0)
    total_used = Decimal(0)
    for entry in payload.get("vms") or []:
        name = str(entry.get("name") or "").strip()
        if not name:
            raise ConfigError("a vm has no name")
        if name in vms:  # the result is keyed by name, a dup would eat a vm silently
            raise ConfigError(f"duplicate vm name {name!r}")
        provisioned = _usage(entry.get("provisioned"), f"{name}.provisioned")
        # nothing measured -> charge what was reserved, never invent waste
        measured = entry.get("used")
        used = _usage(measured, f"{name}.used") if measured is not None else provisioned

        provisioned_cost = _cost(provisioned, rates, hours)
        used_cost = _cost(used, rates, hours)
        vms[name] = {
            "provisioned": float(provisioned_cost),
            "used": float(used_cost),
            "waste": float(provisioned_cost - used_cost),
        }
        for field in _USAGE:
            reserved[field] += provisioned[field]
        total_provisioned += provisioned_cost
        total_used += used_cost

    return {
        "pool_monthly": float(pool),
        "hours_per_month": float(hours),
        # printed in the report next to every number they produced, they're arguable
        "weights": {key: float(value) for key, value in weights.items()},
        "capacity": {key: float(value) for key, value in capacity.items()},
        "rates": {key: float(value) for key, value in rates.items()},
        "vms": vms,
        "total_provisioned": float(total_provisioned),
        "total_used": float(total_used),
        "headroom": float(pool - total_provisioned),
        "allocated_ratio": float(total_provisioned / pool),
        "overcommit": {
            "vcpu": float(reserved["vcpu"] / capacity["vcpu"]),
            "ram": float(reserved["ram_gib"] / capacity["ram_gib"]),
            "storage": float(reserved["disk_gib"] / capacity["storage_gib"]),
        },
    }


def pool_monthly(card: dict) -> Decimal:
    """What one pool costs its owner per month. public so config can reject a dead card early."""
    if not card:
        raise ConfigError("rate card is empty, nothing to allocate")
    unknown = sorted(set(card) - set(COST_LINES) - set(CAPEX_LINES))
    if unknown:
        raise ConfigError(f"unknown rate-card line(s): {', '.join(unknown)}")

    total = sum((_non_negative(card[key], key) for key in COST_LINES if key in card), Decimal(0))
    if (capex := card.get("hardware_capex")) is not None:
        lifetime = card.get("lifetime_months")
        if lifetime is None:
            raise ConfigError("hardware_capex needs lifetime_months to amortize over")
        total += _non_negative(capex, "hardware_capex") / _positive(lifetime, "lifetime_months")
    # a lifetime with no capex comes from the site card and has nothing to divide

    if total <= 0:
        raise ConfigError("rate card totals 0, nothing to allocate")
    return total


def _capacity(raw: dict) -> dict[str, Decimal]:
    # unknown keys are fine here: capacity is ours, not the operator's, and a collector
    # may well carry sockets/cores alongside
    capacity = {}
    for key in _CAPACITY:
        if key not in raw:
            raise ConfigError(f"capacity.{key} is missing")
        value = _non_negative(raw[key], f"capacity.{key}")
        if value == 0:
            raise ConfigError(f"capacity.{key} is zero, inventory failed")
        capacity[key] = value
    return capacity


def _weights(raw: dict | None) -> dict[str, Decimal]:
    raw = DEFAULT_WEIGHTS if raw is None else raw
    if missing := [key for key in _WEIGHTS if key not in raw]:
        raise ConfigError(f"weights missing: {', '.join(missing)}")
    if unknown := sorted(set(raw) - set(_WEIGHTS)):
        raise ConfigError(f"unknown weight(s): {', '.join(unknown)}")
    weights = {key: _non_negative(raw[key], f"weights.{key}") for key in _WEIGHTS}
    # they split one pool between them, so they have to add up to it
    if abs(sum(weights.values()) - 1) > _WEIGHT_SUM_TOLERANCE:
        raise ConfigError(f"weights must sum to 1.0, got {sum(weights.values())}")
    return weights


def _usage(raw: object, where: str) -> dict[str, Decimal]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} is missing")
    # a field left out is zero, a vm with no disk of its own is normal
    return {key: _non_negative(raw.get(key, 0), f"{where}.{key}") for key in _USAGE}


def _cost(usage: dict[str, Decimal], rates: dict[str, Decimal], hours: Decimal) -> Decimal:
    return (
        usage["vcpu"] * hours * rates["vcpu_hour"]
        + usage["ram_gib"] * hours * rates["ram_gib_hour"]
        + usage["disk_gib"] * rates["storage_gib_month"]
    )


def _number(value: object, where: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        raise ConfigError(f"{where} is not a number: {value!r}")
    try:
        # str() first, Decimal(0.1) would carry the float's binary error into money
        number = Decimal(str(value))
    except ArithmeticError:
        raise ConfigError(f"{where} is not a number: {value!r}") from None
    if not number.is_finite():
        raise ConfigError(f"{where} is not finite: {value!r}")
    return number


def _non_negative(value: object, where: str) -> Decimal:
    number = _number(value, where)
    if number < 0:
        raise ConfigError(f"{where} is negative: {value!r}")
    return number


def _positive(value: object, where: str) -> Decimal:
    number = _number(value, where)
    if number <= 0:
        raise ConfigError(f"{where} must be above zero, got {value!r}")
    return number
