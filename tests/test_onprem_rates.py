"""On-prem rate-card allocation: the division, and what it refuses to divide.

The full golden table lives in the cft functests (it drives this through the image);
here is one scenario of it plus every guard, so `pytest -q` stays the gate.
"""

from __future__ import annotations

import pytest

from clont.core.errors import ConfigError
from clont.finops.onprem.rates import allocate

CENT = 0.005
REL = 1e-9

CARD = {
    "hardware_amortization": 12000,
    "power_and_cooling": 3400,
    "rack_and_network": 1500,
    "licenses": 2000,
    "staff": 0,
}
CAPACITY = {"vcpu": 96, "ram_gib": 1536, "storage_gib": 20480}


def payload(**over):
    base = {
        "hours_per_month": 730,
        "weights": {"cpu": 0.5, "ram": 0.3, "storage": 0.2},
        "rate_card": CARD,
        "capacity": CAPACITY,
        "vms": [
            {
                "name": "app-01",
                "provisioned": {"vcpu": 4, "ram_gib": 16, "disk_gib": 100},
                "used": {"vcpu": 0.4, "ram_gib": 3, "disk_gib": 40},
            },
        ],
    }
    return base | over


def test_rates_and_costs_match_the_golden_row():
    got = allocate(payload())
    assert got["pool_monthly"] == pytest.approx(18900, abs=CENT)
    assert got["rates"]["vcpu_hour"] == pytest.approx(0.134845890411, rel=REL)
    assert got["rates"]["ram_gib_hour"] == pytest.approx(0.005056720890, rel=REL)
    assert got["rates"]["storage_gib_month"] == pytest.approx(0.1845703125, rel=REL)
    assert got["vms"]["app-01"] == {
        "provisioned": pytest.approx(471.269531, abs=CENT),
        "used": pytest.approx(57.832031, abs=CENT),
        "waste": pytest.approx(413.437500, abs=CENT),
    }


def test_capex_over_a_lifetime_is_the_hardware_line():
    card = {"hardware_capex": 480000, "lifetime_months": 48, "staff": 4000}
    got = allocate(payload(rate_card=card))
    assert got["pool_monthly"] == pytest.approx(14000, abs=CENT)


def test_capex_without_a_lifetime_raises():
    with pytest.raises(ConfigError, match="lifetime_months"):
        allocate(payload(rate_card={"hardware_capex": 480000}))


def test_overcommit_is_reported_not_clamped():
    fleet = [
        {
            "name": f"fleet-{tag}",
            "provisioned": {"vcpu": 96, "ram_gib": 768, "disk_gib": 10240},
            "used": {"vcpu": 10, "ram_gib": 300, "disk_gib": 8000},
        }
        for tag in ("a", "b")
    ]
    got = allocate(payload(vms=fleet))
    assert got["total_provisioned"] == pytest.approx(28350, abs=CENT)
    assert got["headroom"] == pytest.approx(-9450, abs=CENT)
    assert got["allocated_ratio"] == pytest.approx(1.5, rel=REL)
    assert got["overcommit"]["vcpu"] == pytest.approx(2.0, rel=REL)


def test_rates_ignore_what_the_vms_ask_for():
    """Rates come from the pool and the capacity only, never from demand."""
    empty = allocate(payload(vms=[]))
    busy = allocate(
        payload(
            vms=[{"name": "big", "provisioned": {"vcpu": 96, "ram_gib": 1536, "disk_gib": 20480}}]
        )
    )
    assert empty["rates"] == busy["rates"]


def test_unmeasured_vm_is_charged_on_what_it_reserved():
    got = allocate(payload(vms=[{"name": "app-01", "provisioned": {"vcpu": 4, "ram_gib": 16}}]))
    line = got["vms"]["app-01"]
    assert line["used"] == pytest.approx(line["provisioned"], abs=CENT)
    assert line["waste"] == pytest.approx(0.0, abs=CENT)


def test_doubling_every_line_doubles_every_number():
    base = allocate(payload())
    twice = allocate(payload(rate_card={key: value * 2 for key, value in CARD.items()}))
    assert twice["pool_monthly"] == pytest.approx(base["pool_monthly"] * 2, abs=CENT)
    for key, value in base["rates"].items():
        assert twice["rates"][key] == pytest.approx(value * 2, rel=REL), key
    # a ratio is dimensionless, it must not move
    assert twice["allocated_ratio"] == pytest.approx(base["allocated_ratio"], rel=REL)


@pytest.mark.parametrize("dimension", ["vcpu", "ram_gib", "storage_gib"])
def test_zero_capacity_raises(dimension):
    with pytest.raises(ConfigError, match="inventory failed"):
        allocate(payload(capacity=CAPACITY | {dimension: 0}))


@pytest.mark.parametrize("card", [{}, {"staff": 0}])
def test_card_with_no_money_on_it_raises(card):
    with pytest.raises(ConfigError, match="nothing to allocate"):
        allocate(payload(rate_card=card))


def test_missing_capacity_dimension_raises():
    with pytest.raises(ConfigError, match="capacity.storage_gib is missing"):
        allocate(payload(capacity={"vcpu": 96, "ram_gib": 1536}))


def test_typo_on_the_card_raises_instead_of_lowering_the_pool():
    with pytest.raises(ConfigError, match="licences"):
        allocate(payload(rate_card=CARD | {"licences": 2000}))


def test_weights_that_do_not_sum_to_one_raise():
    with pytest.raises(ConfigError, match="sum to 1.0"):
        allocate(payload(weights={"cpu": 0.5, "ram": 0.3, "storage": 0.1}))


def test_unknown_weight_raises():
    with pytest.raises(ConfigError, match="gpu"):
        allocate(payload(weights={"cpu": 0.5, "ram": 0.3, "storage": 0.2, "gpu": 0.0}))


def test_duplicate_vm_name_raises():
    vm = {"name": "app-01", "provisioned": {"vcpu": 1}}
    with pytest.raises(ConfigError, match="duplicate"):
        allocate(payload(vms=[vm, vm]))


def test_negative_cost_line_raises():
    with pytest.raises(ConfigError, match="negative"):
        allocate(payload(rate_card=CARD | {"licenses": -2000}))
