"""The payer's linked accounts, from AWS Organizations.

`organizations:ListAccounts` is free and only the management account can call it.
Two things are built on it:

- names for the bare 12-digit ids in a payer's CUR, so per-account spend reads as
  `staging` rather than `481516234290`
- the member list clont fans out over when `members.role_name` is set

Optional by design: without the grant (or outside an org) ids are used as-is and
spend still reports, so this never becomes a reason a cycle fails.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from clont.core.logging import get_logger

log = get_logger("clont.providers.aws.organizations")

# names change about never; re-listing every cycle is pointless
_TTL_SECONDS = 6 * 3600

_cache: dict[str, tuple[float, list["OrgAccount"]]] = {}


@dataclass(frozen=True, slots=True)
class OrgAccount:
    id: str
    alias: str     # slug of the org account name, used as clont's account alias
    name: str      # as spelled in Organizations
    status: str    # ACTIVE / SUSPENDED / PENDING_CLOSURE


def clear_cache() -> None:
    _cache.clear()


def accounts(provider) -> list[OrgAccount]:
    """Every account in the org, or an empty list when the call isn't available."""
    key = str(getattr(provider, "account_id", None) or provider.alias)
    hit = _cache.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < _TTL_SECONDS:
        return hit[1]

    found: list[OrgAccount] = []
    try:
        client = provider.client("organizations", "us-east-1")
        for page in client.get_paginator("list_accounts").paginate():
            for account in page.get("Accounts", []):
                ident = str(account.get("Id") or "")
                name = str(account.get("Name") or "").strip()
                if not ident:
                    continue
                found.append(
                    OrgAccount(
                        id=ident,
                        alias=_slug(name) or ident,
                        name=name or ident,
                        status=str(account.get("Status") or ""),
                    )
                )
    except Exception as exc:  # noqa: BLE001 - this is cosmetic, ids still work
        # denied, not a payer, or no org at all: same outcome here
        log.info(
            "%s: no organizations account list (%s) — linked accounts are named by id",
            provider.alias,
            exc,
        )

    _cache[key] = (now, found)
    return found


def account_names(provider) -> dict[str, str]:
    """account id -> alias. suspended accounts are kept: they still carry spend."""
    return {a.id: a.alias for a in accounts(provider)}


def _slug(name: str) -> str:
    """Org names are free text; aliases end up in event keys and budget rules."""
    out = "".join(c if c.isalnum() else "-" for c in name.lower())
    return "-".join(part for part in out.split("-") if part)
