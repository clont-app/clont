"""EC2 finops: instance spend + idle/rightsizing recommendations."""

from __future__ import annotations

from clont.core.models import Cloud, Period
from clont.core.registry import register
from clont.finops.models import CostRecord, Recommendation
from clont.providers.base import Provider


@register("finops", Cloud.AWS, "ec2")
class EC2CostCollector:
    cloud = Cloud.AWS
    service = "ec2"

    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    def collect(self, period: Period) -> list[CostRecord]:
        # TODO: correlate Cost Explorer EC2 spend with describe_instances.
        raise NotImplementedError

    def recommendations(self, period: Period) -> list[Recommendation]:
        # TODO: flag stopped-but-billed EBS, idle instances (low CloudWatch CPU).
        raise NotImplementedError
