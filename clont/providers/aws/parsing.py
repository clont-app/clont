"""Pydantic models for parsing raw AWS API responses."""

from __future__ import annotations
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field


class _CEAmount(BaseModel):
    """A Cost Explorer metric value, e.g. ``{"Amount": "12.34", "Unit": "USD"}``."""

    model_config = ConfigDict(populate_by_name=True)

    amount: Decimal = Field(alias="Amount")
    unit: str = Field(alias="Unit")


class _CEGroup(BaseModel):
    """One ``GroupBy`` bucket of a Cost Explorer result (here: by SERVICE)."""

    model_config = ConfigDict(populate_by_name=True)

    keys: list[str] = Field(alias="Keys")
    metrics: dict[str, _CEAmount] = Field(alias="Metrics")
