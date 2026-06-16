"""Unit tests for the Cost Explorer parsing models."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from clont.providers.aws.parsing import _CEAmount, _CEGroup


def test_amount_coerces_string_to_decimal():
    amount = _CEAmount.model_validate({"Amount": "12.34", "Unit": "USD"})
    assert amount.amount == Decimal("12.34")
    assert amount.unit == "USD"


def test_group_maps_aliases():
    group = _CEGroup.model_validate(
        {
            "Keys": ["Amazon EC2"],
            "Metrics": {"UnblendedCost": {"Amount": "1.5", "Unit": "USD"}},
        }
    )
    assert group.keys == ["Amazon EC2"]
    assert group.metrics["UnblendedCost"].amount == Decimal("1.5")


def test_group_rejects_missing_keys():
    with pytest.raises(ValidationError):
        _CEGroup.model_validate({"Metrics": {}})
