"""Site -> cluster rate-card merge: the pool a cluster gets priced on.

The merge is config, not a third arithmetic path — it hands `allocate()` one flat card.
So what is tested here is which lines survive, and that a card which cannot produce a
price fails on load rather than at the first collection.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from clont.core.config import Config
from clont.finops.onprem.config import OnPremSite
from clont.finops.onprem.rates import allocate, pool_monthly

# the plan's example: the floor's lines on the site, the iron per cluster
SITE = {
    "rate_card": {
        "power_and_cooling": 3400,
        "rack_and_network": 1500,
        "licenses": 2000,
        "lifetime_months": 48,
    },
    "clusters": {
        "prod-gen11": {"rate_card": {"hardware_capex": 480000, "licenses": 6000}},
        "dev-gen9": {"rate_card": {"hardware_capex": 120000, "lifetime_months": 60}},
        "plain": {},
    },
}


def test_cluster_overrides_the_site_line_by_line():
    pools = OnPremSite(**SITE).pools()

    prod = pools["prod-gen11"]["rate_card"]
    assert prod["licenses"] == 6000        # its own
    assert prod["power_and_cooling"] == 3400  # the floor's
    assert prod["lifetime_months"] == 48

    dev = pools["dev-gen9"]["rate_card"]
    assert dev["lifetime_months"] == 60    # replaced, not added to
    assert dev["licenses"] == 2000         # untouched by its neighbour


def test_a_cluster_with_no_overrides_gets_the_site_card():
    site = OnPremSite(**SITE)
    assert site.pool("plain") == site.pool("anything-the-collector-finds")
    assert site.pool("plain")["rate_card"] == site.rate_card.lines()


def test_amortization_on_a_cluster_drops_the_sites_capex():
    # two spellings of one line: merging per key would bill the hardware twice
    site = OnPremSite(
        rate_card={"hardware_capex": 480000, "lifetime_months": 48, "licenses": 2000},
        clusters={"leased": {"rate_card": {"hardware_amortization": 1000}}},
    )
    card = site.pool("leased")["rate_card"]

    assert "hardware_capex" not in card
    assert pool_monthly(card) == 3000  # 1000 + 2000, not 10000 + 1000 + 2000


def test_capex_on_a_cluster_drops_the_sites_amortization():
    site = OnPremSite(
        rate_card={"hardware_amortization": 1000},
        clusters={"owned": {"rate_card": {"hardware_capex": 48000, "lifetime_months": 48}}},
    )
    assert pool_monthly(site.pool("owned")["rate_card"]) == 1000  # 48000/48, the site line gone


def test_weights_come_from_the_site_unless_the_cluster_says_otherwise():
    site = OnPremSite(
        rate_card={"licenses": 10},
        weights={"cpu": 0.6, "ram": 0.2, "storage": 0.2},
        clusters={
            "inherits": {},
            "storage-heavy": {"weights": {"cpu": 0.2, "ram": 0.2, "storage": 0.6}},
        },
    )
    assert site.pool("inherits")["weights"]["cpu"] == Decimal("0.6")
    assert site.pool("storage-heavy")["weights"]["storage"] == Decimal("0.6")


def test_merged_card_feeds_the_allocator():
    # same pool as the golden table's dc2 scenario, assembled from two levels
    site = OnPremSite(
        rate_card={"power_and_cooling": 2000, "rack_and_network": 1000, "lifetime_months": 48},
        clusters={"dc2": {"rate_card": {"hardware_capex": 480000, "licenses": 3000, "staff": 4000}}},
    )
    pool = site.pool("dc2")
    result = allocate(
        {
            **pool,
            "capacity": {"vcpu": 200, "ram_gib": 1024, "storage_gib": 10240},
            "vms": [
                {
                    "name": "big-a",
                    "provisioned": {"vcpu": 100, "ram_gib": 512, "disk_gib": 5120},
                }
            ],
        }
    )
    assert result["pool_monthly"] == 20000
    assert result["total_provisioned"] == pytest.approx(10000, abs=0.005)


@pytest.mark.parametrize(
    "site",
    [
        {"rate_card": {}},                                    # nothing at all
        {"rate_card": {"lifetime_months": 48}},               # a divisor and nothing to divide
        {"rate_card": {"licenses": 0}},                       # totals zero
        {"rate_card": {"hardware_capex": 1000}},              # capex with no lifetime
    ],
)
def test_a_card_that_cannot_price_anything_fails_on_load(site):
    with pytest.raises(ValidationError):
        OnPremSite(**site)


def test_a_fragment_site_card_is_fine_once_clusters_carry_the_iron():
    # on its own this site prices nothing, and that is legal: every cluster is named
    site = OnPremSite(
        rate_card={"lifetime_months": 48},
        clusters={"a": {"rate_card": {"hardware_capex": 480000}}},
    )
    assert pool_monthly(site.pool("a")["rate_card"]) == 10000


def test_a_cluster_left_unpriced_fails_on_load():
    with pytest.raises(ValidationError):
        OnPremSite(
            rate_card={"lifetime_months": 48},
            clusters={"a": {"rate_card": {"hardware_capex": 480000}}, "forgotten": {}},
        )


def test_a_mistyped_line_is_rejected_not_dropped():
    # a dropped key would quietly lower the pool, and every number under it
    with pytest.raises(ValidationError):
        OnPremSite(rate_card={"power_and_colling": 3400})


def test_weights_must_sum_to_one():
    with pytest.raises(ValidationError):
        OnPremSite(rate_card={"licenses": 10}, weights={"cpu": 0.5, "ram": 0.5, "storage": 0.2})


_YAML = """\
onprem:
  dc1:
    rate_card:
      power_and_cooling: 3400
      lifetime_months: 48
    clusters:
      prod-gen11:
        rate_card: {hardware_capex: 480000}
"""


def test_config_loads_the_alias_keyed_onprem_map(tmp_path, monkeypatch):
    cfg_file = tmp_path / "clont.yaml"
    cfg_file.write_text(_YAML)
    monkeypatch.setenv("CLONT_CONFIG", str(cfg_file))

    config = Config()

    assert list(config.onprem) == ["dc1"]
    assert pool_monthly(config.onprem["dc1"].pool("prod-gen11")["rate_card"]) == 13400
