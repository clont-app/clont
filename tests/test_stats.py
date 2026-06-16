"""Classic-stats helpers: median, MAD, modified z-score (float + Decimal)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from clont.events.stats import mad, mean, median, modified_zscore


def test_median_odd_and_even():
    assert median([3, 1, 2]) == 2
    assert median([1, 2, 3, 4]) == 2.5


def test_median_and_mean_preserve_decimal():
    assert median([Decimal("1"), Decimal("3")]) == Decimal("2")
    assert mean([Decimal("2"), Decimal("4")]) == Decimal("3")
    assert isinstance(median([Decimal("1"), Decimal("3")]), Decimal)


def test_mad_is_robust_spread():
    # MAD ignores the single far outlier that would blow up std.
    assert mad([1, 1, 1, 1, 100]) == 0  # 4 of 5 equal the median -> MAD 0
    assert mad([1, 2, 3, 4, 5]) == 1


def test_modified_zscore_flags_outlier():
    z = modified_zscore(100, [10, 11, 9, 10, 12])
    assert z is not None and abs(z) > 5


def test_modified_zscore_none_on_flat_cohort():
    assert modified_zscore(9, [5, 5, 5, 5]) is None  # MAD 0 -> can't judge


def test_modified_zscore_none_on_empty():
    assert modified_zscore(1, []) is None


def test_empty_inputs_raise():
    with pytest.raises(ValueError):
        median([])
    with pytest.raises(ValueError):
        mean([])
