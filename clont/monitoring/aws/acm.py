"""ACM monitoring: certificate expiry.

Zero per-resource config — the warn/critical day thresholds are shipped
defaults (the "target" is expiry, not a user-set number).
"""

from __future__ import annotations

from datetime import UTC, datetime

from clont.core.models import Cloud, CloudResource
from clont.core.registry import register
from clont.monitoring.aws._common import for_each_region
from clont.monitoring.models import HealthCheck, HealthStatus
from clont.providers.aws.parsing import _ACMCertificate
from clont.providers.base import Provider

_WARN_DAYS = 30
_CRIT_DAYS = 7


@register("monitoring", Cloud.AWS, "acm")
class ACMHealthCollector:
    cloud = Cloud.AWS
    service = "acm"

    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    def health(self) -> list[HealthCheck]:
        return for_each_region(self._provider, self._region_health, what="acm health")

    def _region_health(self, region: str) -> list[HealthCheck]:
        acm = self._provider.client("acm", region)
        checks: list[HealthCheck] = []
        for page in acm.get_paginator("list_certificates").paginate():
            for summary in page.get("CertificateSummaryList", []):
                arn = summary["CertificateArn"]
                raw = acm.describe_certificate(CertificateArn=arn)["Certificate"]
                cert = _ACMCertificate.model_validate(raw)
                if cert.not_after is None:  # not yet issued / no validity window
                    continue
                status, text = self._evaluate(cert.not_after)
                checks.append(
                    HealthCheck(
                        resource=CloudResource(
                            cloud=Cloud.AWS,
                            service="acm",
                            # Key on the ARN, not the domain: certs can share a
                            # domain (renewals, wildcard+apex, dup imports) and a
                            # domain-keyed event would let one cert mask another's
                            # expiry. The domain is carried in the summary.
                            resource_id=arn,
                            region=region,
                            alias=self._provider.alias,
                        ),
                        status=status,
                        summary=f"{cert.domain or arn}: {text}",
                    )
                )
        return checks

    @staticmethod
    def _evaluate(not_after: datetime) -> tuple[HealthStatus, str]:
        days = (not_after - datetime.now(UTC)).days
        if days < 0:
            return HealthStatus.CRITICAL, f"expired {abs(days)}d ago"
        if days <= _CRIT_DAYS:
            return HealthStatus.CRITICAL, f"expires in {days}d"
        if days <= _WARN_DAYS:
            return HealthStatus.WARN, f"expires in {days}d"
        return HealthStatus.OK, f"expires in {days}d"
