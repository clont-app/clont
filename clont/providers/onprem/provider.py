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

One thing points the other way: **a guest platform knows something about this site that
vcenter will not say.** A kubernetes volume nothing has attached is datastore space
belonging to no vm, which from here is indistinguishable from an iso or a dead vm folder.
So a cluster priced by this site registers itself with `attach_guest`, and the waste pass
subtracts what it owns up to instead of offering the same blocks back twice. The hooks are
plain callables and one that raises is dropped — a cluster is never the site's dependency.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from clont.core.errors import ConfigError
from clont.core.logging import get_logger
from clont.core.models import Cloud
from clont.finops.guests import GuestStorage
from clont.finops.onprem.config import InventoryConfig, OnPremSite
from clont.providers.onprem.inventory import SiteInventory
from clont.providers.onprem.vsphere import BROWSE_GRANT, VsphereInventory

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
        self._browsed: tuple[float, SiteInventory] | None = None
        # what a peer's gc needs: vms with their devices, no browse, no perf
        self._peer_pass: tuple[float, SiteInventory] | None = None
        self._guests: list[tuple[str, Callable[[], GuestStorage]]] = []
        # the other sites clont reads, a vcenter sharing an array with this one
        self._peers: list[tuple[str, Callable[[], SiteInventory]]] = []
        # Datastore.Browse, checked once per run: datastores never to browse again, and
        # whether that is all of them
        self._browse_checked = False
        self._browse_skip: frozenset[str] = frozenset()
        self._browse_off = False
        # the clone events failed once, warned, not asked again this run
        self._clones_off = False
        # same for cns
        self._cns_off = False

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
            site = session.site(
                usage_window_days=self._config.usage_window_days, clones=not self._clones_off
            )
            self._clones_off = self._clones_off or session.clones_failed
        self._last = (self._clock(), site)
        return site

    def browsed_inventory(self) -> SiteInventory:
        """The pass plus the datastore file list, for the waste pass and nobody else.

        Its own cache: the hourly cost pass has no use for a browse task per datastore,
        and must not wait on one. `inventory.browse: false` makes this `inventory()`.
        """
        if not self._config.browse or self._browse_off:
            # gc won't run on this site, but a peer's gc may still want our vms
            return self.inventory()
        if self._browsed is not None and self._clock() - self._browsed[0] < self._ttl:
            return self._browsed[1]
        with self._session() as session:
            self._check_browse(session)
            site = session.site(
                usage_window_days=self._config.usage_window_days,
                browse=not self._browse_off,
                browse_skip=self._browse_skip,
                clones=not self._clones_off,
                # cns volumes are judged against the clusters' pvs, none attached -> no read
                cns=bool(self._guests) and not self._cns_off,
                devices=True,
            )
            self._clones_off = self._clones_off or session.clones_failed
            self._cns_off = self._cns_off or session.cns_failed
            # refused inside the task: browse() warned this once, never ask again
            self._skip_browse(session.browse_denied, site)
        now = self._clock()
        self._browsed = (now, site)
        self._last = (now, site)
        return site

    def peer_inventory(self) -> SiteInventory:
        """our vms with their device backings, for another site's gc. the browsed pass
        when it's fresh, else its own short one with no browse and no perf"""
        now = self._clock()
        for cached in (self._browsed, self._peer_pass):
            if cached is not None and now - cached[0] < self._ttl:
                return cached[1]
        with self._session() as session:
            site = session.site(devices=True)
        self._peer_pass = (self._clock(), site)
        return site

    def _check_browse(self, session: VsphereInventory) -> None:
        """Ask once per run whether the login may browse, warn once if not."""
        if self._browse_checked:
            return
        self._browse_checked = True
        grants = session.browse_grants()
        if not grants:
            # the host can't say, the first browse finds out per datastore
            return
        denied = sorted(name for name, ok in grants.items() if not ok)
        if not denied:
            return
        self._browse_skip = frozenset(denied)
        if len(denied) == len(grants):
            self._browse_off = True
            log.warning(
                "%s: %s has no %s, datastore gc is off for this run — grant it to the "
                "clont role for the file list, snapshots still work",
                self.alias,
                self._config.username,
                BROWSE_GRANT,
            )
        else:
            log.warning(
                "%s: no %s on %s, those datastores are not browsed this run",
                self.alias,
                BROWSE_GRANT,
                ", ".join(denied),
            )

    def _skip_browse(self, denied: list[str], site: SiteInventory) -> None:
        if not denied:
            return
        self._browse_skip = self._browse_skip | frozenset(denied)
        names = {ds.name for ds in site.datastores}
        if names and names <= self._browse_skip:
            self._browse_off = True

    def attach_guest(self, name: str, reader: Callable[[], GuestStorage]) -> None:
        """Register a guest platform that holds space on this site's datastores.

        Wired by `bootstrap` for every kubernetes cluster this site prices. The site cannot
        see which blocks on an array are a cluster's volumes — the cluster can, and the
        storage gap is wrong by that much until it says so.
        """
        self._guests.append((name, reader))

    def guest_storage(self) -> list[GuestStorage]:
        """What every attached guest says it holds here, failures left out.

        One unreachable cluster must not cost the site its whole recommendation pass, so a
        reader that raises is dropped with a warning: the gap then simply stays as large as
        it was before, which is the number clont reported for months.
        """
        out: list[GuestStorage] = []
        for name, reader in self._guests:
            try:
                out.append(reader())
            except Exception as exc:  # noqa: BLE001 - one guest is not the site
                log.warning("%s: guest %s not reconciled: %s", self.alias, name, exc)
        return out

    def attach_peer(self, alias: str, reader: Callable[[], SiteInventory]) -> None:
        """Register another site clont reads. wired by `bootstrap` for every pair.

        a vcenter only names its own hosts as a datastore's mounts, so an array two
        vcenters share looks private from either side. the peer's vms are what keep its
        files live for datastore gc here
        """
        self._peers.append((alias, reader))

    def peer_sites(self) -> list[tuple[str, SiteInventory | None]]:
        """every peer's last pass, none for one whose read failed"""
        out: list[tuple[str, SiteInventory | None]] = []
        for alias, reader in self._peers:
            try:
                out.append((alias, reader()))
            except Exception as exc:  # noqa: BLE001 - one peer is not the site
                log.warning(
                    "%s: peer site %s not read, gc doubts its files: %s", self.alias, alias, exc
                )
                out.append((alias, None))
        return out

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
            with self._session() as session:
                site = session.site(usage_window_days=self._config.usage_window_days)
                self._last = (self._clock(), site)
                if self._config.browse and site.pools:
                    self._check_browse(session)
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
        if self._browse_skip:
            # optional, gc only. the rest of the pass is whole without it
            return [
                f"{self._config.username}: {BROWSE_GRANT} (optional, datastore gc) on "
                + ", ".join(sorted(self._browse_skip))
            ]
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
