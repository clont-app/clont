"""The guard that keeps Cost Explorer out of a default cycle.

Nothing calls `ce:*` unless the operator opts in with
`finops.allow_cost_explorer`: the recommendation collectors were moved onto free
describes, and spend now comes from the CUR. Without this test a future
collector quietly re-adds a billed call per cycle.

The provider *records* the services asked for rather than raising on `ce`:
`for_each_region` swallows per-region exceptions by design, so a raise here would
be caught by the very isolation it's meant to see through, and the test would
pass for the wrong reason.
"""

from __future__ import annotations

import pytest

from clont.core import registry
from clont.core.models import Cloud, Period
from clont.finops.aws import inventory
from clont.finops.aws.cost_explorer import CostExplorerCollector
from clont.finops.base import FinOpsTuning
from tests.test_finops_inventory import FakeProvider, instance


class _Anything:
    """Answers any read with an empty result, so a collector runs to the end."""

    def __getattr__(self, name: str):
        if name == "get_paginator":
            return lambda *a, **kw: _Anything()
        return lambda *a, **kw: {}

    def paginate(self, *a, **kw):
        return iter([{}])


class _Recording(FakeProvider):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.asked: list[str] = []

    def client(self, service: str, region: str | None = None):
        self.asked.append(service)
        try:
            return super().client(service, region)
        except AssertionError:
            return _Anything()  # a service this fake doesn't model


def _period() -> Period:
    from datetime import date

    return Period(date(2026, 1, 1), date(2026, 1, 31))


@pytest.fixture(autouse=True)
def _no_cache():
    inventory.clear_cache()
    yield
    inventory.clear_cache()


@pytest.mark.parametrize(
    "cls",
    list(registry.collectors_for("finops", Cloud.AWS)),
    ids=lambda c: c.service,
)
def test_no_collector_calls_cost_explorer(cls):
    provider = _Recording(instances={"us-east-1": [instance("i-1")]})
    collector = cls(provider)
    try:
        collector.recommendations(_period())
        collector.collect(_period())
    except NotImplementedError:
        pytest.skip(f"{cls.service} is still a stub")
    assert "ce" not in provider.asked


def test_the_guard_would_notice_a_cost_explorer_call():
    # Proof the recording provider sees through for_each_region's isolation.
    provider = _Recording()
    CostExplorerCollector(provider, FinOpsTuning(allow_cost_explorer=True)).collect(_period())
    assert "ce" in provider.asked
