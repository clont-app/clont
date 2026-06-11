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


class _EBSVolume(BaseModel):
    """One ``describe_volumes`` entry (for waste detection, not health)."""

    volume_id: str = Field(validation_alias="VolumeId")
    size: int = Field(default=0, validation_alias="Size")
    volume_type: str = Field(default="", validation_alias="VolumeType")
    state: str = Field(default="", validation_alias="State")


class _EIP(BaseModel):
    """One ``describe_addresses`` entry; unassociated if ``association_id`` is empty."""

    public_ip: str = Field(default="", validation_alias="PublicIp")
    allocation_id: str = Field(default="", validation_alias="AllocationId")
    association_id: str = Field(default="", validation_alias="AssociationId")


class _NatGateway(BaseModel):
    """One ``describe_nat_gateways`` entry (for idle detection)."""

    nat_gateway_id: str = Field(validation_alias="NatGatewayId")
    state: str = Field(default="", validation_alias="State")


class _LoadBalancer(BaseModel):
    """One ``describe_load_balancers`` entry (ELBv2: ALB/NLB)."""

    arn: str = Field(default="", validation_alias="LoadBalancerArn")
    name: str = Field(default="", validation_alias="LoadBalancerName")
    lb_type: str = Field(default="", validation_alias="Type")
    state: str = Field(default="", validation_alias=AliasPath("State", "Code"))


class _Snapshot(BaseModel):
    """One ``describe_snapshots`` entry (self-owned EBS snapshots)."""

    snapshot_id: str = Field(validation_alias="SnapshotId")
    volume_id: str = Field(default="", validation_alias="VolumeId")
    volume_size: int = Field(default=0, validation_alias="VolumeSize")
    start_time: datetime | None = Field(default=None, validation_alias="StartTime")


class _COOption(BaseModel):
    """One Compute Optimizer recommendationOption: target + monthly savings.

    Shared by EC2 and EBS responses (EBS has no ``instanceType``, so it defaults
    to empty). Savings are nested under ``savingsOpportunity``.
    """

    rank: int = Field(default=0, validation_alias="rank")
    instance_type: str = Field(default="", validation_alias="instanceType")
    savings: Decimal = Field(
        default=Decimal(0),
        validation_alias=AliasPath("savingsOpportunity", "estimatedMonthlySavings", "value"),
    )
    currency: str = Field(
        default="USD",
        validation_alias=AliasPath("savingsOpportunity", "estimatedMonthlySavings", "currency"),
    )
    pct: float = Field(
        default=0.0,
        validation_alias=AliasPath("savingsOpportunity", "savingsOpportunityPercentage"),
    )


class _COInstanceRec(BaseModel):
    """One ``get_ec2_instance_recommendations`` entry."""

    arn: str = Field(default="", validation_alias="instanceArn")
    name: str = Field(default="", validation_alias="instanceName")
    current_type: str = Field(default="", validation_alias="currentInstanceType")
    finding: str = Field(default="", validation_alias="finding")
    options: list[_COOption] = Field(default_factory=list, validation_alias="recommendationOptions")


class _COVolumeRec(BaseModel):
    """One ``get_ebs_volume_recommendations`` entry."""

    arn: str = Field(default="", validation_alias="volumeArn")
    finding: str = Field(default="", validation_alias="finding")
    options: list[_COOption] = Field(
        default_factory=list, validation_alias="volumeRecommendationOptions"
    )


class _COASGRec(BaseModel):
    """One ``get_auto_scaling_group_recommendations`` entry."""

    arn: str = Field(default="", validation_alias="autoScalingGroupArn")
    finding: str = Field(default="", validation_alias="finding")
    options: list[_COOption] = Field(
        default_factory=list, validation_alias="recommendationOptions"
    )


class _COLambdaRec(BaseModel):
    """One ``get_lambda_function_recommendations`` entry."""

    arn: str = Field(default="", validation_alias="functionArn")
    finding: str = Field(default="", validation_alias="finding")
    options: list[_COOption] = Field(
        default_factory=list, validation_alias="memorySizeRecommendationOptions"
    )


class _COECSRec(BaseModel):
    """One ``get_ecs_service_recommendations`` entry."""

    arn: str = Field(default="", validation_alias="serviceArn")
    finding: str = Field(default="", validation_alias="finding")
    options: list[_COOption] = Field(
        default_factory=list, validation_alias="recommendationOptions"
    )


class _CORDSRec(BaseModel):
    """One ``get_rds_database_recommendations`` entry (instance rightsizing).

    RDS uses ``instanceFinding`` (not ``finding``) and a separate
    ``instanceRecommendationOptions`` list; storage recommendations are ignored.
    """

    arn: str = Field(default="", validation_alias="resourceArn")
    finding: str = Field(default="", validation_alias="instanceFinding")
    options: list[_COOption] = Field(
        default_factory=list, validation_alias="instanceRecommendationOptions"
    )


class _SPSummary(BaseModel):
    """The aggregate summary of a Savings Plans purchase recommendation.
    """

    model_config = ConfigDict(populate_by_name=True)

    estimated_monthly_savings: Decimal = Field(
        default=Decimal(0), validation_alias="EstimatedMonthlySavingsAmount"
    )
    savings_pct: Decimal = Field(
        default=Decimal(0), validation_alias="EstimatedSavingsPercentage"
    )
    hourly_commitment: Decimal = Field(
        default=Decimal(0), validation_alias="HourlyCommitmentToPurchase"
    )
    currency: str = Field(default="USD", validation_alias="CurrencyCode")


class _RIRecommendation(BaseModel):
    """One ``get_reservation_purchase_recommendation`` entry (its summary).

    Savings live under the nested ``RecommendationSummary`` object.
    """

    model_config = ConfigDict(populate_by_name=True)

    estimated_monthly_savings: Decimal = Field(
        default=Decimal(0),
        validation_alias=AliasPath("RecommendationSummary", "TotalEstimatedMonthlySavingsAmount"),
    )
    savings_pct: Decimal = Field(
        default=Decimal(0),
        validation_alias=AliasPath("RecommendationSummary", "TotalEstimatedMonthlySavingsPercentage"),
    )
    currency: str = Field(
        default="USD",
        validation_alias=AliasPath("RecommendationSummary", "CurrencyCode"),
    )


class _HealthEvent(BaseModel):
    """One ``describe_events`` entry (AWS Health). Lowercase keys."""

    arn: str = Field(default="", validation_alias="arn")
    service: str = Field(default="", validation_alias="service")
    category: str = Field(default="", validation_alias="eventTypeCategory")
    status_code: str = Field(default="", validation_alias="statusCode")
    region: str = Field(default="", validation_alias="region")
