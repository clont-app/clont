"""Collector registry: registration, lookup, and per-(domain, cloud) listing.

A private `domain` ("test") keeps these assertions isolated from the real
finops/monitoring collectors that import-time-register into the same global map.
"""

from __future__ import annotations

import pytest

from clont.core import registry
from clont.core.models import Cloud


def test_register_and_get_roundtrip():
    @registry.register("test", Cloud.AWS, "widget")
    class Widget:
        pass

    assert registry.get("test", Cloud.AWS, "widget") is Widget


def test_register_returns_class_unchanged():
    # The decorator must return the class untouched (usable as a normal class).
    @registry.register("test", Cloud.GCP, "gadget")
    class Gadget:
        value = 7

    assert Gadget.value == 7
    assert Gadget().value == 7


def test_get_missing_raises_keyerror():
    with pytest.raises(KeyError):
        registry.get("test", Cloud.AWS, "does-not-exist")


def test_collectors_for_filters_by_domain_and_cloud():
    @registry.register("test", Cloud.AWS, "a")
    class A:
        pass

    @registry.register("test", Cloud.AWS, "b")
    class B:
        pass

    @registry.register("test", Cloud.AZURE, "c")
    class C:
        pass

    aws = set(registry.collectors_for("test", Cloud.AWS))
    assert A in aws and B in aws
    assert C not in aws                       # different cloud excluded
    assert set(registry.collectors_for("test", Cloud.AZURE)) == {C}


def test_real_finops_collectors_are_registered():
    # Importing the package registers every finops collector; spot-check a few.
    import clont.finops.aws  # noqa: F401

    services = {
        svc for (dom, cloud, svc) in registry._REGISTRY if dom == "finops" and cloud == Cloud.AWS
    }
    assert {"s3", "ec2", "waste"} <= services
