"""The one statistic clont argues about, in one place.

Lives here because both measured layers need the same answer and must not drift apart: the
vsphere perf pass (`providers/onprem/metrics.py`) and the kubernetes usage pass
(`providers/k8s/usage.py`) size iron off it, and two percentile implementations would size
the same workload two ways.
"""

from __future__ import annotations

from decimal import Decimal
from math import ceil

DEFAULT_QUANTILE = Decimal("0.95")


def percentile(values: list[Decimal], quantile: Decimal = DEFAULT_QUANTILE) -> Decimal:
    """Nearest-rank percentile: the smallest sample at or above the quantile's rank.

    No interpolation on purpose — it is defined on 3 samples as well as on 3000, and the
    answer is always a number something actually reported.
    """
    if not values:
        return Decimal(0)
    ordered = sorted(values)
    rank = max(1, min(len(ordered), ceil(float(quantile) * len(ordered))))
    return ordered[rank - 1]
