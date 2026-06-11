"""FinOps result types."""

from __future__ import annotations

from dataclasses import dataclass

from clont.core.models import CloudResource, Money, Period


@dataclass(frozen=True, slots=True)
class CostRecord:
    """Cost attributed to a service (and optionally a specific resource)."""

    cloud: str
    service: str
    period: Period
    cost: Money
    alias: str | None = None  # account alias the cost belongs to
    resource: CloudResource | None = None
    dimensions: dict[str, str] | None = None  # e.g. {"usage_type": "..."}


@dataclass(frozen=True, slots=True)
class Recommendation:
    """A cost-saving suggestion (e.g. idle resource, rightsizing).
    """

    cloud: str
    service: str
    resource: CloudResource
    summary: str
    estimated_savings: Money
    kind: str = "rec"
