"""Pydantic models for parsing raw AWS API responses."""

from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from pydantic import AliasPath, BaseModel, ConfigDict, Field


class _CEAmount(BaseModel):
    """A Cost Explorer metric value, e.g. ``{"Amount": "12.34", "Unit": "USD"}``."""

    model_config = ConfigDict(populate_by_name=True)

    amount: Decimal = Field(alias="Amount")
    unit: str = Field(alias="Unit")


class _CEGroup(BaseModel):
    """One ``GroupBy`` bucket of a Cost Explorer result (here: by SERVICE)."""

    model_config = ConfigDict(populate_by_name=True)

    keys: list[str] = Field(alias="Keys")
    metrics: dict[str, _CEAmount] = Field(alias="Metrics")


class _EC2Status(BaseModel):
    """One ``describe_instance_status`` entry: the two reachability checks."""

    instance_id: str = Field(validation_alias="InstanceId")
    system_status: str = Field(validation_alias=AliasPath("SystemStatus", "Status"))
    instance_status: str = Field(validation_alias=AliasPath("InstanceStatus", "Status"))


class _RDSInstance(BaseModel):
    """One ``describe_db_instances`` entry: identity + lifecycle status."""

    instance_id: str = Field(validation_alias="DBInstanceIdentifier")
    status: str = Field(validation_alias="DBInstanceStatus")


class _CacheCluster(BaseModel):
    """One ``describe_cache_clusters`` entry: identity + lifecycle status."""

    cluster_id: str = Field(validation_alias="CacheClusterId")
    status: str = Field(validation_alias="CacheClusterStatus")


class _EKSClusterIssue(BaseModel):
    code: str = Field(default="", validation_alias="code")
    message: str = Field(default="", validation_alias="message")


class _EKSCluster(BaseModel):
    """The ``describe_cluster`` ``cluster`` object: status + health issues.

    EKS responses use lowercase keys (``name``, ``status``, ``health``).
    """

    name: str = Field(validation_alias="name")
    status: str = Field(validation_alias="status")
    issues: list[_EKSClusterIssue] = Field(
        default_factory=list, validation_alias=AliasPath("health", "issues")
    )


class _EBSVolumeStatus(BaseModel):
    """One ``describe_volume_status`` entry."""

    volume_id: str = Field(validation_alias="VolumeId")
    status: str = Field(validation_alias=AliasPath("VolumeStatus", "Status"))


class _RedshiftCluster(BaseModel):
    """One ``describe_clusters`` entry (Redshift)."""

    cluster_id: str = Field(validation_alias="ClusterIdentifier")
    availability: str = Field(default="", validation_alias="ClusterAvailabilityStatus")


class _ASGInstance(BaseModel):
    health: str = Field(default="", validation_alias="HealthStatus")
    lifecycle: str = Field(default="", validation_alias="LifecycleState")


class _ASG(BaseModel):
    """One ``describe_auto_scaling_groups`` entry."""

    name: str = Field(validation_alias="AutoScalingGroupName")
    desired: int = Field(default=0, validation_alias="DesiredCapacity")
    instances: list[_ASGInstance] = Field(default_factory=list, validation_alias="Instances")


class _TargetHealth(BaseModel):
    """One ``describe_target_health`` description (ELBv2)."""

    state: str = Field(default="", validation_alias=AliasPath("TargetHealth", "State"))


class _ECSDeployment(BaseModel):
    rollout_state: str = Field(default="", validation_alias="rolloutState")


class _ECSService(BaseModel):
    """One ``describe_services`` entry (ECS). Lowercase keys."""

    name: str = Field(validation_alias="serviceName")
    running: int = Field(default=0, validation_alias="runningCount")
    desired: int = Field(default=0, validation_alias="desiredCount")
    deployments: list[_ECSDeployment] = Field(default_factory=list, validation_alias="deployments")


class _ACMCertificate(BaseModel):
    """The ``describe_certificate`` ``Certificate`` object."""

    domain: str = Field(default="", validation_alias="DomainName")
    status: str = Field(default="", validation_alias="Status")
    not_after: datetime | None = Field(default=None, validation_alias="NotAfter")


class _HealthEvent(BaseModel):
    """One ``describe_events`` entry (AWS Health). Lowercase keys."""

    arn: str = Field(default="", validation_alias="arn")
    service: str = Field(default="", validation_alias="service")
    category: str = Field(default="", validation_alias="eventTypeCategory")
    status_code: str = Field(default="", validation_alias="statusCode")
    region: str = Field(default="", validation_alias="region")
