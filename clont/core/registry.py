"""Collector registry."""

from __future__ import annotations
from collections.abc import Iterable
from clont.core.models import Cloud

# (domain, cloud, service) -> collector class
_REGISTRY: dict[tuple[str, Cloud, str], type] = {}


def register(domain: str, cloud: Cloud, service: str):
    """Class decorator that records a collector in the registry."""

    def decorator[T](cls: type[T]) -> type[T]:
        _REGISTRY[(domain, cloud, service)] = cls
        return cls

    return decorator


def get(domain: str, cloud: Cloud, service: str) -> type:
    return _REGISTRY[(domain, cloud, service)]


def collectors_for(domain: str, cloud: Cloud) -> Iterable[type]:
    for (d, c, _service), cls in _REGISTRY.items():
        if d == domain and c == cloud:
            yield cls
