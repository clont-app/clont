"""Shared value types used by both finops and monitoring domains.

These are deliberately cloud-agnostic so that `reporting/` can render results
from any provider uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


class Cloud(StrEnum):
    AWS = "aws"
    GCP = "gcp"
    AZURE = "azure"


@dataclass(frozen=True, slots=True)
class Money:
    """A monetary amount in a given currency."""

    amount: Decimal
    currency: str = "USD"


@dataclass(frozen=True, slots=True)
class Period:
    """A closed date range [start, end] used to scope queries."""

    start: date
    end: date


@dataclass(frozen=True, slots=True)
class CloudResource:
    """Identity of a single cloud resource, used to correlate finops + monitoring."""

    cloud: Cloud
    service: str          # e.g. "ec2", "s3"
    resource_id: str      # e.g. instance id, bucket name
    region: str | None = None
    account: str | None = None