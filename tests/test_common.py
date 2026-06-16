"""for_each_region: flatten across regions, isolate a failing one."""

from __future__ import annotations

from botocore.exceptions import ClientError

from clont.monitoring.aws._common import for_each_region


class _FakeProvider:
    def __init__(self, regions: list[str], alias: str = "prod") -> None:
        self._regions = regions
        self.alias = alias

    def regions(self) -> list[str]:
        return self._regions


def test_flattens_results_across_regions():
    provider = _FakeProvider(["us-east-1", "eu-west-1"])
    out = for_each_region(provider, lambda r: [f"{r}:a", f"{r}:b"], what="t")
    assert out == ["us-east-1:a", "us-east-1:b", "eu-west-1:a", "eu-west-1:b"]


def test_client_error_region_is_skipped():
    provider = _FakeProvider(["bad", "good"])

    def fn(region: str) -> list[str]:
        if region == "bad":
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "Describe")
        return [region]

    assert for_each_region(provider, fn, what="t") == ["good"]


def test_non_client_error_region_is_also_isolated():
    # A malformed response that trips parsing (not a ClientError) must not sink
    # the other regions.
    provider = _FakeProvider(["bad", "good"])

    def fn(region: str) -> list[str]:
        if region == "bad":
            raise KeyError("unexpected response shape")
        return [region]

    assert for_each_region(provider, fn, what="t") == ["good"]
