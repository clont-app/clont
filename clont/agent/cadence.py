"""How often a collector is actually allowed to hit the cloud.

The loop wakes every `interval_seconds` (300 by default) but almost nothing it
asks for changes that fast: spend data lands once a day, Compute Optimizer
recommendations once every few hours. Calling anyway is what turns a $0.01 API
into a $960/month one.

`Due` is the memo that fixes it — it reuses the last result until the ttl
expires. Reuse, not skip: a collector that goes quiet for 287 of 288 cycles
would empty `batch.costs` and the digest would silently vanish.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Hashable

# spend data updates daily; recommendations a few times a day
DEFAULT_COLLECT_SECONDS = 86_400
DEFAULT_RECOMMEND_SECONDS = 3_600

Key = tuple[Hashable, ...]


def ttl_for(cls: type, attr: str, default: int) -> int:
    """Read a collector's cadence off the class.

    Collectors are duck-typed through `registry` and don't inherit
    `CostCollector`, so a default on the Protocol does nothing at runtime — it
    has to be a `getattr`. A class attr beats the operator's default, which is
    the point: the collector knows how stale its upstream is.
    """
    return getattr(cls, attr, default)


class Due:
    """Process-local, no disk — a fresh process is a cold cache, deliberately.

    That's why `clont run --once` needs no special casing, and why the first
    cycle of `run_forever` refreshes everything.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[Key, tuple[float, Any]] = {}

    def last(self, key: Key) -> Any | None:
        """The last good value, or None if this key has never succeeded."""
        entry = self._entries.get(key)
        return entry[1] if entry is not None else None

    def get_or_reuse(self, key: Key, ttl: float, fn: Callable[[], Any], *, force: bool = False) -> Any:
        """Call `fn` if the entry is missing, stale or forced; else reuse it.

        A raising `fn` propagates and leaves the entry untouched — the caller
        decides whether to fall back to `last()`, because only it knows whether
        an empty result is safe.
        """
        now = self._clock()
        entry = self._entries.get(key)
        if entry is not None and not force and now - entry[0] < ttl:
            return entry[1]
        value = fn()
        self._entries[key] = (now, value)
        return value
