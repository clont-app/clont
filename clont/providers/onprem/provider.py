"""One site of own iron, behind the same `Provider` protocol as an aws account.

The mapping that makes on-prem fit a cloud-shaped interface:

| protocol | here |
|---|---|
| `alias` | the site (`dc1`), exactly as in `onprem:` in the config |
| `account_id` | the vcenter's instance uuid — it survives a rename, the endpoint does not |
| `regions()` | the pools, i.e. clusters and standalone hosts |
| `client(service)` | a backend session; `"inventory"` is the only service there is |

It carries the whole site config, rate cards included, the same way `AWSProvider.cur`
carries a billing location: the collector gets handed a provider and nothing else, so
anything it needs to price what it finds has to be reachable from here.

**Sessions are short-lived, by one pass.** vcenter expires an idle session (30 minutes by
default) and the collector runs hourly, so a session held open between cycles is a
session that is dead when it is next needed. Every pass logs in and logs out, which is
also what an operator watching the session list expects to see.

So the caching is on the *result*, not the connection: `inventory()` keeps the last pass
for `pass_ttl_seconds` so `collect()` and `recommendations()` in the same cycle share one
login instead of walking the whole vcenter twice.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from clont.core.errors import ConfigError
from clont.core.logging import get_logger
from clont.core.models import Cloud
from clont.finops.onprem.config import InventoryConfig, OnPremSite
from clont.providers.onprem.inventory import SiteInventory
from clont.providers.onprem.vsphere import VsphereInventory

log = get_logger("clont.providers.onprem")

PASS_TTL_SECONDS = 300
INVENTORY = "inventory"


class OnPremProvider:
    cloud = Cloud.ONPREM

    def __init__(
        self,
        alias: str,
        site: OnPremSite,
        *,
        pass_ttl_seconds: float = PASS_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if site.inventory is None:
            raise ConfigError(f"onprem.{alias} has no inventory block, nothing to read")
        self.alias = alias
        self.account_id: str | None = None  # vcenter instance uuid, set in authenticate()
        self._site = site
        self._config: InventoryConfig = site.inventory
        self._ttl = pass_ttl_seconds
        self._clock = clock
        self._last: tuple[float, SiteInventory] | None = None

    @property
    def site(self) -> OnPremSite:
        """The rate cards for this site — what prices whatever the pass finds."""
        return self._site

    def authenticate(self) -> None:
        """Log in once and out again, to fail at startup rather than at the first pass."""
        with self._session() as session:
            self.account_id = session.instance_uuid
        log.info("authenticated %s at %s (%s)", self.alias, self._config.endpoint, self.account_id)

    def inventory(self, *, refresh: bool = False) -> SiteInventory:
        """One pass over the site, measured usage included, reused within the ttl.

        The perf read rides on the same session on purpose: it is the expensive half of
        the pass and both collectors want it, so paying for it twice an hour would be two
        logins and two walks of the whole vcenter for one set of numbers.
        """
        if not refresh and self._last is not None:
            age = self._clock() - self._last[0]
            if age < self._ttl:
                return self._last[1]
        with self._session() as session:
            site = session.site(usage_window_days=self._config.usage_window_days)
        self._last = (self._clock(), site)
        return site

    def regions(self) -> list[str]:
        """The pools. `region` is the protocol's word for "where", and here that is a cluster."""
        return [pool.key for pool in self.inventory().pools]

    def client(self, service: str, region: str | None = None) -> Any:
        """A fresh, *unconnected* backend session. The caller owns it — use `with`."""
        if service != INVENTORY:
            raise ConfigError(f"on-prem has no {service!r} client, only {INVENTORY!r}")
        return self._session()

    def preflight(self) -> list[str]:
        """What the read-only account still cannot see (empty == it has what clont needs)."""
        try:
            site = self.inventory(refresh=True)
        except ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - pyvmomi faults are only importable with the extra
            if "NoPermission" not in type(exc).__name__:
                raise
            return [f"{self._config.username}: the Read-Only role on the vcenter root"]
        if not site.pools:
            # the login worked and nothing came back, so the role is granted on some child
            # object instead of the root folder — propagating it is the fix
            return [f"{self._config.username}: no cluster or host is visible"]
        return []

    def _session(self) -> VsphereInventory:
        if self._config.kind != "vsphere":
            raise ConfigError(f"unsupported on-prem inventory kind {self._config.kind!r}")
        return VsphereInventory(
            self._config.endpoint,
            self._config.username,
            # read at use time, never stored on the provider: one less copy to leak
            self._config.secret(),
            port=self._config.port,
            verify_ssl=self._config.verify_ssl,
            ca_bundle=self._config.ca_bundle,
        )
