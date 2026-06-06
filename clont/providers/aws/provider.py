"""AWS provider: assume the configured read-only role and vend boto3 clients.

This is the single place that handles AWS auth. Finops and monitoring AWS
collectors both receive an already-authenticated instance of this class.

Credentials are *refreshable*: the agent is long-running, so assuming the role
once would break after the temporary credentials expire (~1h). We hand boto3 a
``DeferredRefreshableCredentials`` backed by an ``AssumeRoleCredentialFetcher``,
which re-assumes the role transparently whenever the cached credentials are near
expiry.
"""

from __future__ import annotations

from typing import Any

import boto3
import botocore.session
from botocore.credentials import (
    AssumeRoleCredentialFetcher,
    DeferredRefreshableCredentials,
)
from botocore.exceptions import ClientError

from clont.core.config import AWSConfig
from clont.core.logging import get_logger
from clont.core.models import Cloud

log = get_logger("clont.providers.aws")

# IAM denies surface under different error codes depending on the service.
_DENIED_CODES = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}


def _build_session(config: AWSConfig) -> boto3.Session:
    """Build a boto3 Session with auto-refreshing assume-role credentials.

    The base identity comes from the default credential chain (IRSA on EKS,
    env vars or instance profile locally); from there we assume the configured
    cross-account read-only role.
    """
    base = botocore.session.get_session()  # default chain = pod/host identity

    extra_args: dict[str, str] = {"RoleSessionName": "clont"}
    if config.external_id:
        extra_args["ExternalId"] = config.external_id

    fetcher = AssumeRoleCredentialFetcher(
        client_creator=base.create_client,
        source_credentials=base.get_credentials(),
        role_arn=config.role_arn,
        extra_args=extra_args,
    )
    creds = DeferredRefreshableCredentials(
        method="sts-assume-role",
        refresh_using=fetcher.fetch_credentials,
    )

    botocore_session = botocore.session.get_session()
    botocore_session._credentials = creds
    return boto3.Session(botocore_session=botocore_session)


class AWSProvider:
    cloud = Cloud.AWS

    def __init__(self, config: AWSConfig) -> None:
        self._config = config
        self._session: boto3.Session | None = None
        self._clients: dict[tuple[str, str | None], Any] = {}

    def authenticate(self) -> None:
        """Assume `config.role_arn` and cache a refreshable session.

        Fails fast: a bad role ARN, missing trust or denied ``sts:AssumeRole``
        raises here so the agent stops at startup with a clear error, rather
        than silently producing no data every cycle.
        """
        self._session = _build_session(self._config)
        ident = self._session.client("sts").get_caller_identity()
        log.info("authenticated as %s", ident["Arn"])

    def regions(self) -> list[str]:
        """Regions in scope: the configured list, or every enabled region."""
        if self._config.regions:
            return list(self._config.regions)
        ec2 = self.client("ec2", "us-east-1")
        resp = ec2.describe_regions(
            Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]
        )
        return [r["RegionName"] for r in resp["Regions"]]

    def client(self, service: str, region: str | None = None) -> Any:
        """Return a boto3 client for `service` in `region` (cached).

        Clients are cached per (service, region); the agent loop is
        single-threaded so reuse is safe and avoids rebuilding clients each
        cycle. Credentials behind the shared session still refresh on their own.
        """
        if self._session is None:
            raise RuntimeError("authenticate() must be called first")
        key = (service, region)
        client = self._clients.get(key)
        if client is None:
            client = self._session.client(service, region_name=region)
            self._clients[key] = client
        return client

    def preflight(self) -> list[str]:
        """Probe a few read-only calls and report missing permissions.

        Returns the list of API actions that came back AccessDenied (empty ==
        the role has what clont needs). Non-permission errors propagate.
        """
        missing: list[str] = []
        probes = [
            ("ce:GetCostAndUsage", self._probe_cost_explorer),
            ("ec2:DescribeRegions", self._probe_describe_regions),
        ]
        for action, probe in probes:
            try:
                probe()
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in _DENIED_CODES:
                    missing.append(action)
                    continue
                raise
        return missing

    def _probe_cost_explorer(self) -> None:
        from datetime import date, timedelta

        today = date.today()
        self.client("ce", "us-east-1").get_cost_and_usage(
            TimePeriod={
                "Start": (today - timedelta(days=1)).isoformat(),
                "End": today.isoformat(),
            },
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
        )

    def _probe_describe_regions(self) -> None:
        self.client("ec2", "us-east-1").describe_regions()
