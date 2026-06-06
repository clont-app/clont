"""S3 finops: storage spend + lifecycle/storage-class recommendations."""

from __future__ import annotations

from clont.core.models import Cloud, Period
from clont.core.registry import register
from clont.finops.models import CostRecord, Recommendation
from clont.providers.base import Provider


@register("finops", Cloud.AWS, "s3")
class S3CostCollector:
    cloud = Cloud.AWS
    service = "s3"

    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    def collect(self, period: Period) -> list[CostRecord]:
        raise NotImplementedError

    def recommendations(self, period: Period) -> list[Recommendation]:
        # TODO: buckets without lifecycle rules, large Standard tier cold data.
        raise NotImplementedError
