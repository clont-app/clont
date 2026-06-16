"""ElastiCache health collector: status mapping + attribution, via Stubber."""

from __future__ import annotations

import boto3
from botocore.stub import Stubber

from clont.events.detectors import HealthDetector
from clont.monitoring.aws.elasticache import ElastiCacheHealthCollector
from clont.monitoring.models import HealthStatus


class _FakeProvider:
    def __init__(self, client, alias: str = "prod") -> None:
        self._client = client
        self.alias = alias

    def regions(self) -> list[str]:
        return ["eu-west-1"]

    def client(self, service: str, region: str | None = None):
        assert service == "elasticache"
        return self._client


def test_elasticache_health_maps_status_and_attributes():
    client = boto3.client(
        "elasticache", region_name="eu-west-1",
        aws_access_key_id="x", aws_secret_access_key="x",
    )
    with Stubber(client) as stubber:
        stubber.add_response(
            "describe_cache_clusters",
            {
                "CacheClusters": [
                    {"CacheClusterId": "cc-ok", "CacheClusterStatus": "available"},
                    {"CacheClusterId": "cc-bad", "CacheClusterStatus": "incompatible-network"},
                    {"CacheClusterId": "cc-new", "CacheClusterStatus": "creating"},
                ]
            },
        )
        checks = ElastiCacheHealthCollector(_FakeProvider(client)).health()

    by_id = {c.resource.resource_id: c for c in checks}
    assert by_id["cc-ok"].status is HealthStatus.OK
    assert by_id["cc-bad"].status is HealthStatus.CRITICAL
    assert by_id["cc-new"].status is HealthStatus.UNKNOWN
    assert all(c.resource.region == "eu-west-1" and c.resource.alias == "prod" for c in checks)

    events = HealthDetector().detect(checks)
    assert {e.key for e in events} == {"monitoring:health:prod:aws:elasticache:cc-bad"}
