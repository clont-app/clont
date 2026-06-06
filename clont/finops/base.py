"""Cost collector protocol.

Each finops service module implements one collector and registers it via
`@register("finops", Cloud.X, "service")`.
"""

from __future__ import annotations

from typing import Protocol

from clont.core.models import Cloud, Period
from clont.finops.models import CostRecord, Recommendation
from clont.providers.base import Provider


class CostCollector(Protocol):
    cloud: Cloud
    service: str

    def __init__(self, provider: Provider) -> None: ...

    def collect(self, period: Period) -> list[CostRecord]:
        """Return cost records for the given period."""
        ...

    def recommendations(self, period: Period) -> list[Recommendation]:
        """Return optimization suggestions (may be empty)."""
        ...
