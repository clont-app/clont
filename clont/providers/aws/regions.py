"""Region traversal shared by AWS collectors (monitoring and finops alike)."""

from __future__ import annotations

from collections.abc import Callable

from clont.core.logging import get_logger
from clont.providers.base import Provider

log = get_logger("clont.providers.aws.regions")


def for_each_region[T](
    provider: Provider, fn: Callable[[str], list[T]], *, what: str
) -> list[T]:
    """Run `fn(region)` for every region in scope, flattening the results.

    A region that raises (AccessDenied or a disabled region from `ClientError`,
    a service that isn't opted in, or a malformed response that trips parsing)
    is logged and skipped so one bad region can't sink the others — the
    isolation pattern every regional collector shares.
    """
    out: list[T] = []
    for region in provider.regions():
        try:
            out.extend(fn(region))
        except Exception as exc:  # noqa: BLE001 - isolate one region, keep the rest
            log.warning("%s: skipping region %s for %s: %s", what, region, provider.alias, exc)
    return out
