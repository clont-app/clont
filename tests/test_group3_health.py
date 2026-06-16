"""Group 3 account-level signals: ACM cert expiry + AWS Health events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from botocore.exceptions import ClientError

from clont.monitoring.aws.acm import ACMHealthCollector
from clont.monitoring.aws.health import HealthEventsCollector
from clont.monitoring.models import HealthStatus


class _Paginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **kwargs):
        yield from self._pages


class _FakeClient:
    def __init__(self, paginators: dict | None = None, **methods) -> None:
        self._paginators = paginators or {}
        self._methods = methods

    def get_paginator(self, name: str) -> _Paginator:
        return _Paginator(self._paginators[name])

    def __getattr__(self, name: str):
        handler = self.__dict__["_methods"][name]
        return handler if callable(handler) else (lambda **kw: handler)


class _FakeProvider:
    def __init__(self, clients: dict, alias: str = "prod") -> None:
        self._clients = clients
        self.alias = alias

    def regions(self) -> list[str]:
        return ["us-east-1"]

    def client(self, service: str, region: str | None = None):
        return self._clients[service]


def test_acm_expiry_thresholds():
    now = datetime.now(UTC)
    certs = {
        "arn-expired": {"DomainName": "expired.example", "NotAfter": now - timedelta(days=1)},
        "arn-crit": {"DomainName": "crit.example", "NotAfter": now + timedelta(days=3)},
        "arn-warn": {"DomainName": "warn.example", "NotAfter": now + timedelta(days=20)},
        "arn-ok": {"DomainName": "ok.example", "NotAfter": now + timedelta(days=100)},
        "arn-pending": {"DomainName": "pending.example"},  # no NotAfter -> skipped
    }

    def describe_certificate(CertificateArn, **kw):
        return {"Certificate": certs[CertificateArn]}

    acm = _FakeClient(
        paginators={
            "list_certificates": [
                {"CertificateSummaryList": [{"CertificateArn": a} for a in certs]}
            ]
        },
        describe_certificate=describe_certificate,
    )
    checks = ACMHealthCollector(_FakeProvider({"acm": acm})).health()

    # Keyed on the cert ARN (not the domain) so same-domain certs can't mask
    # one another; the domain is carried in the summary.
    by_arn = {c.resource.resource_id: c.status for c in checks}
    assert by_arn == {
        "arn-expired": HealthStatus.CRITICAL,
        "arn-crit": HealthStatus.CRITICAL,
        "arn-warn": HealthStatus.WARN,
        "arn-ok": HealthStatus.OK,
    }  # arn-pending skipped (no NotAfter)
    summaries = {c.resource.resource_id: c.summary for c in checks}
    assert summaries["arn-warn"].startswith("warn.example:")


def test_aws_health_events_mapped():
    health = _FakeClient(
        paginators={
            "describe_events": [
                {
                    "events": [
                        {
                            "arn": "evt-issue",
                            "service": "EC2",
                            "eventTypeCategory": "issue",
                            "statusCode": "open",
                            "region": "us-east-1",
                        },
                        {
                            "arn": "evt-change",
                            "service": "RDS",
                            "eventTypeCategory": "scheduledChange",
                            "statusCode": "upcoming",
                            "region": "us-west-2",
                        },
                    ]
                }
            ]
        }
    )
    checks = HealthEventsCollector(_FakeProvider({"health": health})).health()
    by_id = {c.resource.resource_id: c.status for c in checks}
    assert by_id == {"evt-issue": HealthStatus.CRITICAL, "evt-change": HealthStatus.WARN}


def test_aws_health_subscription_denied_returns_empty():
    class _DenyClient:
        def get_paginator(self, name):
            raise ClientError(
                {"Error": {"Code": "SubscriptionRequiredException", "Message": "no support plan"}},
                "DescribeEvents",
            )

    checks = HealthEventsCollector(_FakeProvider({"health": _DenyClient()})).health()
    assert checks == []
