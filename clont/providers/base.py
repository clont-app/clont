"""Provider protocol shared by all clouds."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from clont.core.models import Cloud


@runtime_checkable
class Provider(Protocol):
    """Connectivity for a single cloud account.

    Implementations live in `providers/<cloud>/provider.py`.
    """

    cloud: Cloud
    alias: str             # human account name from config (e.g. "prod")
    account: str | None    # native account id (AWS account / GCP project )

    def authenticate(self) -> None:
        """Establish credentials (e.g. assume the read-only role)."""
        ...

    def regions(self) -> list[str]:
        """Regions in scope for this account/profile."""
        ...

    def client(self, service: str, region: str | None = None) -> Any:
        """Return a low-level SDK client for `service` (e.g. boto3 client)."""
        ...

    def preflight(self) -> list[str]:
        """Return a list of missing read-only permissions (empty == OK)."""
        ...
