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

    def __init__(self, alias: str, config: AWSConfig) -> None:
        self.alias = alias
        self.account_id: str | None = None  # AWS account id, set in authenticate()
        self._config = config
        self._session: boto3.Session | None = None
        self._clients: dict[tuple[str, str | None], Any] = {}

    def authenticate(self) -> None:
        """Assume `config.role_arn` and cache a refreshable session.

        Raises on a bad role ARN, missing trust or denied ``sts:AssumeRole``;
        the caller (bootstrap) decides whether one bad account is fatal.
        """
        self._session = _build_session(self._config)
        ident = self._session.client("sts").get_caller_identity()
        self.account_id = ident["Account"]
        log.info("authenticated %s as %s (%s)", self.alias, self.account_id, ident["Arn"])

    @property
    def cur(self):
        """This account's Cost and Usage Report location, if one is configured."""
        return self._config.cur

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

        Every probe here is free — `ce:GetCostAndUsage` used to be one of them,
        which meant a $0.01 charge just for checking.
        """
        missing: list[str] = []
        probes = [
            ("ec2:DescribeRegions", self._probe_describe_regions),
            # newest grant, so the one an existing role is most likely missing
            ("savingsplans:DescribeSavingsPlans", self._probe_savings_plans),
            ("compute-optimizer:GetEnrollmentStatus", self._probe_compute_optimizer),
        ]
        if self._config.cur is not None:
            probes.append(("s3:GetObject", self._probe_cur))
        for action, probe in probes:
            try:
                probe()
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in _DENIED_CODES:
                    missing.append(action)
                    continue
                raise
        return missing

    def _probe_cur(self) -> None:
        """Read this month's CUR manifest — proves GetObject on the report."""
        from datetime import date

        from clont.finops.aws.cur import read_manifest

        config = self._config.cur
        manifest = read_manifest(self.client("s3", config.region), config, date.today())
        if manifest is None:
            # not a permission problem: the role can read, there's just nothing
            # delivered yet (a fresh report takes up to 24h)
            log.warning(
                "no CUR manifest yet under s3://%s/%s — spend will be empty until AWS delivers one",
                config.bucket,
                config.prefix,
            )

    def _probe_compute_optimizer(self) -> None:
        """Check the account is opted into Compute Optimizer.

        Enrollment is free but the customer has to flip it, and until they do
        idle/rightsizing recommendations come back empty — which reads exactly
        like "nothing to optimize". Warn so the two are distinguishable.
        """
        resp = self.client("compute-optimizer", "us-east-1").get_enrollment_status()
        status = resp.get("status", "")
        if status != "Active":
            log.warning(
                "%s: compute optimizer is %s — no idle/rightsizing advice until it is "
                "enabled (free); see https://console.aws.amazon.com/compute-optimizer",
                self.alias,
                status or "not enrolled",
            )

    def _probe_describe_regions(self) -> None:
        self.client("ec2", "us-east-1").describe_regions()

    def _probe_savings_plans(self) -> None:
        self.client("savingsplans", "us-east-1").describe_savings_plans(maxResults=1)
