"""The p95 read off prometheus, when a cluster has one.

Three instant queries, no range read: prometheus can compute the percentile itself, so the
answer comes back as one vector per dimension instead of a window of points clont would
have to reduce. stdlib `urllib` on purpose — the same rule as `channels/_http.py`, a
reporting tool does not earn a dependency for one GET.

The queries, and why each piece is in them:

* **`container!=""` is not optional.** cadvisor exports a pod-level roll-up series with an
  empty container label next to the per-container ones; counting both doubles every number
  in the report. Same for the old pause-container series (`container="POD"`).
* **the sum is per pod, then the percentile is of the sum.** A p95 per container summed
  afterwards adds up peaks that never happened together, which is the same mistake as an
  average of averages.
* **cpu needs a subquery** (`[window:step]`): `container_cpu_usage_seconds_total` is a
  counter, so the series being measured is `rate(...)` and that only exists as a subquery.
  It is the expensive query of the three — the step is configurable for exactly that
  reason, and a 5-minute step over 14 days is ~4k points per series.
* **the sample count is its own query**, because "p95 of a series with four points" is a
  guess with a decimal point on it and the vector alone cannot say how long the series is.
  It is a plain `count_over_time`, no subquery, maxed over the pod's containers.

A failed or empty read is not an error here: it means "no row", and `workloads.py` then
advises on nothing rather than on a zero.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation

from clont.core.logging import get_logger
from clont.providers.k8s.nodes import BYTES_PER_GIB
from clont.providers.k8s.usage import PodUsage

log = get_logger("clont.providers.k8s.prometheus")

DEFAULT_WINDOW_DAYS = 14
DEFAULT_STEP_MINUTES = 5
DEFAULT_TIMEOUT = 60  # a 14d subquery over a big cluster is not a fast query
_MAX_RESPONSE_BYTES = 50_000_000

CPU_METRIC = "container_cpu_usage_seconds_total"
RAM_METRIC = "container_memory_working_set_bytes"
# the pod-level roll-up and the old pause container, both of which would double-count
SELECTOR = 'container!="",container!="POD",pod!=""'

CPU_QUERY = (
    'quantile_over_time({q}, sum by (namespace, pod) '
    "(rate({cpu}{{{sel}}}[{step}m]))[{window}d:{step}m])"
)
RAM_QUERY = (
    'quantile_over_time({q}, sum by (namespace, pod) ({ram}{{{sel}}})[{window}d:{step}m])'
)
COUNT_QUERY = "max by (namespace, pod) (count_over_time({ram}{{{sel}}}[{window}d]))"


class Prometheus:
    """One prometheus, asked three questions per pass."""

    def __init__(
        self,
        url: str,
        *,
        window_days: int = DEFAULT_WINDOW_DAYS,
        step_minutes: int = DEFAULT_STEP_MINUTES,
        quantile: Decimal = Decimal("0.95"),
        timeout_seconds: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.url = url.rstrip("/")
        self._window = window_days
        self._step = step_minutes
        self._quantile = quantile
        self._timeout = timeout_seconds

    def pod_usage(self) -> list[PodUsage]:
        """Every pod prometheus has a p95 for, with how many points are behind it."""
        cpu = self._vector(self._query(CPU_QUERY, cpu=CPU_METRIC))
        ram = self._vector(self._query(RAM_QUERY, ram=RAM_METRIC))
        counts = self._vector(self._query(COUNT_QUERY, ram=RAM_METRIC))
        out: list[PodUsage] = []
        for key in sorted(set(cpu) | set(ram)):
            namespace, pod = key
            out.append(
                PodUsage(
                    namespace=namespace,
                    name=pod,
                    vcpu=cpu.get(key, Decimal(0)),
                    ram_gib=ram.get(key, Decimal(0)) / BYTES_PER_GIB,
                    samples=int(counts.get(key, Decimal(0))),
                )
            )
        return out

    def _query(self, template: str, **names: str) -> list[dict]:
        expr = template.format(
            q=self._quantile, sel=SELECTOR, window=self._window, step=self._step, **names
        )
        url = f"{self.url}/api/v1/query?{urllib.parse.urlencode({'query': expr})}"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 - operator-configured url
                raw = resp.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise ValueError(f"prometheus answered over {_MAX_RESPONSE_BYTES} bytes")
            body = json.loads(raw) if raw else {}
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            # a usage read that fails means no advice, never a failed cycle. the expression
            # is logged because a 400 here is almost always a metric name this cluster
            # spells differently
            log.info("%s: usage query failed (%s): %s", self.url, exc, expr)
            return []
        if not isinstance(body, dict) or body.get("status") != "success":
            log.info("%s: usage query answered %s: %s", self.url, body, expr)
            return []
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        result = data.get("result")
        return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []

    @staticmethod
    def _vector(result: list[dict]) -> dict[tuple[str, str], Decimal]:
        """An instant vector as `{(namespace, pod): value}`; a NaN or missing pair is skipped."""
        out: dict[tuple[str, str], Decimal] = {}
        for item in result:
            labels = item.get("metric") if isinstance(item.get("metric"), dict) else {}
            value = item.get("value")
            namespace = str((labels or {}).get("namespace") or "").strip()
            pod = str((labels or {}).get("pod") or "").strip()
            if not namespace or not pod or not isinstance(value, list) or len(value) < 2:
                continue
            number = _decimal(value[1])
            if number is not None:
                out[(namespace, pod)] = number
        return out


def _decimal(raw: object) -> Decimal | None:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    # prometheus answers "NaN" for a quantile of an empty window, and it is not a zero
    return value if value.is_finite() and value >= 0 else None
