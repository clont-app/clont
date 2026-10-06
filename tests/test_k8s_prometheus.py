"""The prometheus p95 read: the queries it sends, and what it does with a bad answer.

The expressions are asserted on purpose. `container!=""` is what keeps cadvisor's pod-level
roll-up out of the sum — without it every number in the report is doubled — and the
percentile has to be *of the per-pod sum*, not a sum of per-container percentiles.

A failed or unparseable read is "no row", never a zero: a zero would read as a workload
that uses nothing and advise shrinking it to the floor.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from clont.providers.k8s.prometheus import Prometheus


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, n: int = -1) -> bytes:
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def vector(*rows: tuple[str, str, str]) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {"namespace": ns, "pod": pod}, "value": [1760000000, value]}
                for ns, pod, value in rows
            ],
        },
    }


def serve(monkeypatch, answers: list[dict]) -> list[str]:
    """Answer the three queries in order and hand back the expressions that were sent."""
    sent: list[str] = []
    pending = list(answers)

    def fake(req, timeout=None):
        sent.append(urllib.parse.unquote_plus(req.full_url.split("query=", 1)[1]))
        return _Resp(json.dumps(pending.pop(0)).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return sent


def test_the_three_queries_are_what_they_claim_to_be(monkeypatch):
    sent = serve(monkeypatch, [vector(), vector(), vector()])
    Prometheus("http://prom:9090/").pod_usage()
    cpu, ram, count = sent
    # the roll-up series is excluded in all three, or every figure is counted twice
    assert all('container!=""' in query for query in sent)
    # the percentile is of the per-pod sum, and cpu needs the subquery because it is a counter
    assert cpu.startswith("quantile_over_time(0.95, sum by (namespace, pod) (rate(")
    assert "[5m]))[14d:5m])" in cpu
    assert ram.startswith("quantile_over_time(0.95, sum by (namespace, pod) (container_memory")
    assert count.startswith("max by (namespace, pod) (count_over_time(")
    assert "[14d]))" in count


def test_the_window_and_step_are_configurable(monkeypatch):
    sent = serve(monkeypatch, [vector(), vector(), vector()])
    Prometheus("http://prom:9090", window_days=7, step_minutes=15).pod_usage()
    assert "[15m]))[7d:15m])" in sent[0]


def test_memory_comes_back_in_gib_and_the_sample_count_rides_along(monkeypatch):
    serve(
        monkeypatch,
        [
            vector(("apps", "web-1", "0.35")),
            vector(("apps", "web-1", str(2 * 1024**3))),
            vector(("apps", "web-1", "4032")),
        ],
    )
    row = Prometheus("http://prom:9090").pod_usage()[0]
    assert (row.namespace, row.name) == ("apps", "web-1")
    assert (row.vcpu, row.ram_gib, row.samples) == (Decimal("0.35"), Decimal(2), 4032)


def test_a_pod_only_one_query_knows_about_still_comes_back(monkeypatch):
    serve(monkeypatch, [vector(("apps", "web-1", "0.5")), vector(), vector()])
    row = Prometheus("http://prom:9090").pod_usage()[0]
    assert (row.vcpu, row.ram_gib, row.samples) == (Decimal("0.5"), Decimal(0), 0)


def test_nan_is_not_a_zero(monkeypatch):
    serve(monkeypatch, [vector(("apps", "web-1", "NaN")), vector(), vector()])
    assert Prometheus("http://prom:9090").pod_usage() == []


def test_a_row_with_no_pod_label_is_skipped(monkeypatch):
    serve(monkeypatch, [vector(("apps", "", "0.5")), vector(), vector()])
    assert Prometheus("http://prom:9090").pod_usage() == []


def test_a_dead_prometheus_means_no_rows_not_a_failed_cycle(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert Prometheus("http://prom:9090").pod_usage() == []


def test_an_error_status_is_not_read_as_data(monkeypatch):
    serve(
        monkeypatch,
        [
            {"status": "error", "errorType": "bad_data", "error": "unknown metric"},
            vector(),
            vector(),
        ],
    )
    assert Prometheus("http://prom:9090").pod_usage() == []
