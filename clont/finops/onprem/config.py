"""Site and cluster rate cards, and the line-by-line merge between them.

`rates.allocate()` takes one flat card for one pool, and **a pool is a cluster** — hardware
generations, licensing and therefore $/vcpu-hour differ per cluster, and averaging them
over a site hides the comparison the report exists for. But an operator should not retype
power, rack and lifetime for every cluster on the floor, so the config is two levels:

    onprem:
      dc1:
        inventory: {kind: vsphere, endpoint: vc1.dc1, username: clont-ro, password_env: VC1_PW}
        rate_card: {power_and_cooling: 3400, rack_and_network: 1500, lifetime_months: 48}
        weights: {cpu: 0.5, ram: 0.3, storage: 0.2}
        clusters:
          prod-gen11: {rate_card: {hardware_capex: 480000, licenses: 6000}}
          dev-gen9:   {rate_card: {hardware_capex: 120000, lifetime_months: 60}}

A cluster card names only what is its own and overrides the site **per line**, never
wholesale — `dev-gen9` above keeps dc1's power and rack and replaces only the lifetime.
A cluster the config never mentions gets the site card as-is, which is what makes
`clusters:` optional: the collector finds the clusters, the config only prices them.

The one trap: `hardware_amortization` and `hardware_capex`/`lifetime_months` are two
spellings of the *same* line. Merging them per key would sum both and bill the iron
twice, so a cluster that names either hardware line drops both of the site's.

This module validates at load time, so a card that cannot produce a price fails on
startup instead of at the first collection — `pool_monthly` is the same function the
allocator uses, so there is one rule, not two that drift.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from clont.core.errors import ConfigError
from clont.finops.onprem.rates import CAPEX_LINES, COST_LINES, pool_monthly

_HARDWARE = ("hardware_amortization", "hardware_capex")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")  # reject unknown keys to catch yaml typos


class RateCard(_Model):
    """Monthly cost lines for one pool. Every line optional — sites and clusters each
    carry a piece, and only the merge has to add up to something."""

    hardware_amortization: Decimal | None = None   # or capex / lifetime below
    hardware_capex: Decimal | None = None
    lifetime_months: Decimal | None = None
    power_and_cooling: Decimal | None = None       # or measured via redfish, later
    rack_and_network: Decimal | None = None
    licenses: Decimal | None = None                # vmware/rhel/windows
    support: Decimal | None = None
    staff: Decimal | None = None                   # off by default, it is arguable

    def lines(self) -> dict[str, Decimal]:
        """Only the lines the operator actually set — an absent line is not a zero."""
        return {key: value for key, value in self.model_dump().items() if value is not None}

    def over(self, base: RateCard) -> RateCard:
        """This card laid over `base`, line by line. Hardware is one line in two spellings."""
        merged = base.lines() | self.lines()
        if any(key in self.lines() for key in _HARDWARE):
            for key in _HARDWARE:
                if key not in self.lines():
                    merged.pop(key, None)
        return RateCard(**merged)


class Weights(_Model):
    """How the pool cost splits over cpu, ram and storage.

    Arguable by design, which is why they live here and get printed in the report beside
    every number they produced.
    """

    cpu: Decimal = Decimal("0.5")
    ram: Decimal = Decimal("0.3")
    storage: Decimal = Decimal("0.2")

    @model_validator(mode="after")
    def _must_sum_to_one(self) -> Weights:
        total = self.cpu + self.ram + self.storage
        if abs(total - 1) > Decimal("0.000001"):
            raise ValueError(f"weights must sum to 1.0, got {total}")
        return self


class ClusterConfig(_Model):
    """What one cluster owns. Anything left out comes from the site."""

    rate_card: RateCard = Field(default_factory=RateCard)
    weights: Weights | None = None


class InventoryConfig(_Model):
    """Where the hypervisor is and which read-only account to read it with.

    The password is not a clont secret store: either it sits in the yaml (which is the
    agent's own file, mode 600) or `password_env` names the variable it arrives in —
    a k8s secret as `secretKeyRef`, systemd's `EnvironmentFile`. Exactly one of the two,
    so a stale inline password can never shadow the env one.
    """

    kind: Literal["vsphere"] = "vsphere"  # libvirt/proxmox join here, same pools out
    endpoint: str                          # vcenter host, no scheme
    username: str                          # the read-only role's account
    password: str | None = None
    password_env: str | None = None
    port: int = Field(default=443, gt=0, lt=65536)
    verify_ssl: bool = True                # a self-signed lab cert is the operator's call
    ca_bundle: str | None = None           # pem for a private ca, instead of turning tls off

    @model_validator(mode="after")
    def _one_password_source(self) -> InventoryConfig:
        if not self.endpoint.strip():
            raise ValueError("inventory.endpoint is empty")
        if bool(self.password) == bool(self.password_env):
            raise ValueError("set exactly one of inventory.password / inventory.password_env")
        return self

    def secret(self) -> str:
        """The password, read at use time — the env may be filled after load."""
        if self.password:
            return self.password
        value = os.environ.get(self.password_env or "", "")
        if not value:
            raise ConfigError(f"{self.password_env} is not set, no vsphere password")
        return value


class OnPremSite(_Model):
    """One site: where to read it, the default card, and the clusters that differ.

    `inventory` is optional on purpose — a site can be priced before anyone hands over a
    read-only account, and the card validates on its own either way.
    """

    inventory: InventoryConfig | None = None
    rate_card: RateCard = Field(default_factory=RateCard)
    weights: Weights = Field(default_factory=Weights)
    clusters: dict[str, ClusterConfig] = Field(default_factory=dict)

    def pool(self, cluster: str) -> dict[str, dict[str, Decimal]]:
        """The flat card + weights for one cluster, ready for `allocate()`.

        An unknown cluster is not an error: it gets the site card, same as a cluster
        listed with no overrides.
        """
        own = self.clusters.get(cluster) or ClusterConfig()
        return {
            "rate_card": own.rate_card.over(self.rate_card).lines(),
            "weights": (own.weights or self.weights).model_dump(),
        }

    def card_for(self, key: str, name: str) -> dict[str, dict[str, Decimal]]:
        """The card for a pool the collector found. A qualified name wins over a bare one.

        Two datacenters behind one vcenter may each hold a "prod" cluster, so an operator
        who has to tell them apart writes `DC0/prod` in `clusters:` and that entry prices
        that one alone.
        """
        return self.pool(key if key in self.clusters else name)

    def pools(self) -> dict[str, dict[str, dict[str, Decimal]]]:
        """Every cluster the config names. Empty when the site card prices all of them."""
        return {name: self.pool(name) for name in self.clusters}

    @model_validator(mode="after")
    def _cards_must_price_something(self) -> OnPremSite:
        # the site card is only checked on its own when it is the only card there is;
        # with clusters named it is free to be a fragment (a lifetime, a power line)
        subjects = self.pools() or {"": self.pool("")}
        for name, pool in subjects.items():
            try:
                pool_monthly(pool["rate_card"])
            except ConfigError as exc:
                where = f"cluster {name!r}" if name else "site rate_card"
                raise ValueError(f"{where}: {exc}") from None
        return self


# re-exported so a reader of the config models can see what a card may hold
__all__ = [
    "CAPEX_LINES",
    "COST_LINES",
    "ClusterConfig",
    "InventoryConfig",
    "OnPremSite",
    "RateCard",
    "Weights",
]
