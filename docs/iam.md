# Read-only IAM setup

clont only ever **reads** from your AWS accounts. For each account you monitor,
create one IAM role that clont assumes, with a read-only permissions policy and
a trust policy that lets clont's runtime identity assume it. The role ARN goes
in `clont.yaml` under the account's alias (see `clont.example.yaml`).

```
runtime identity  ──sts:AssumeRole──►  clont-readonly role (per account)
(IRSA on EKS, or                       read-only permissions below
 an IAM user/role locally)
```

## Permissions policy

Attach this to the `clont-readonly` role. These actions don't support
resource-level scoping, so `Resource` is `*`; they are all read-only.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ClontReadOnly",
      "Effect": "Allow",
      "Action": [
        "ce:GetCostAndUsage",
        "ce:GetSavingsPlansPurchaseRecommendation",
        "ce:GetReservationPurchaseRecommendation",
        "ec2:DescribeRegions",
        "ec2:DescribeInstanceStatus",
        "cloudwatch:GetMetricData",
        "rds:DescribeDBInstances",
        "elasticache:DescribeCacheClusters",
        "eks:ListClusters",
        "eks:DescribeCluster",
        "ec2:DescribeVolumeStatus",
        "redshift:DescribeClusters",
        "autoscaling:DescribeAutoScalingGroups",
        "elasticloadbalancing:DescribeLoadBalancers",
        "elasticloadbalancing:DescribeTargetGroups",
        "elasticloadbalancing:DescribeTargetHealth",
        "ecs:ListClusters",
        "ecs:ListServices",
        "ecs:DescribeServices",
        "acm:ListCertificates",
        "acm:DescribeCertificate",
        "health:DescribeEvents",
        "ec2:DescribeNatGateways",
        "ec2:DescribeSnapshots",
        "compute-optimizer:GetEC2InstanceRecommendations",
        "compute-optimizer:GetEBSVolumeRecommendations",
        "compute-optimizer:GetAutoScalingGroupRecommendations",
        "compute-optimizer:GetLambdaFunctionRecommendations",
        "compute-optimizer:GetECSServiceRecommendations",
        "compute-optimizer:GetRDSDatabaseRecommendations",
        "ec2:DescribeVolumes",
        "ec2:DescribeAddresses"
      ],
      "Resource": "*"
    }
  ]
}
```

The action list grows as you enable more collectors. What each currently
implemented collector needs:

- **Cost Explorer** (account-wide spend) — `ce:GetCostAndUsage`
- **Commitment recommendations** (Savings Plans + Reserved Instances) —
  `ce:GetSavingsPlansPurchaseRecommendation`,
  `ce:GetReservationPurchaseRecommendation` (skipped gracefully when Cost Explorer
  has too little usage data to recommend a purchase)
- **EC2 health** (instance reachability) — `ec2:DescribeInstanceStatus`
- **EC2 metrics** (CPU / network) — `cloudwatch:GetMetricData` (instances are
  discovered via `ec2:DescribeInstanceStatus`)
- **RDS health** (DB instance status) — `rds:DescribeDBInstances`
- **ElastiCache health** (cache cluster status) — `elasticache:DescribeCacheClusters`
- **EKS health** (cluster status / issues) — `eks:ListClusters`, `eks:DescribeCluster`
- **EBS health** (volume status) — `ec2:DescribeVolumeStatus`
- **Redshift health** (cluster availability) — `redshift:DescribeClusters`
- **Auto Scaling health** (healthy vs desired) — `autoscaling:DescribeAutoScalingGroups`
- **Load balancer health** (target health) — `elasticloadbalancing:DescribeTargetGroups`,
  `elasticloadbalancing:DescribeTargetHealth`
- **ECS health** (service running vs desired) — `ecs:ListClusters`, `ecs:ListServices`,
  `ecs:DescribeServices`
- **ACM expiry** (certificate validity) — `acm:ListCertificates`, `acm:DescribeCertificate`
- **AWS Health** (account events) — `health:DescribeEvents` (requires a Business/
  Enterprise Support plan; denied gracefully without one)
- **Compute Optimizer recommendations** (EC2 / EBS / Auto Scaling / Lambda / ECS /
  RDS rightsizing savings) — `compute-optimizer:GetEC2InstanceRecommendations`,
  `compute-optimizer:GetEBSVolumeRecommendations`,
  `compute-optimizer:GetAutoScalingGroupRecommendations`,
  `compute-optimizer:GetLambdaFunctionRecommendations`,
  `compute-optimizer:GetECSServiceRecommendations`,
  `compute-optimizer:GetRDSDatabaseRecommendations` (each resource type is opted
  into Compute Optimizer separately; a type/region that isn't enrolled is skipped
  without affecting the others)
- **Waste recommendations** (unattached EBS, unassociated Elastic IPs, gp2→gp3) —
  `ec2:DescribeVolumes`, `ec2:DescribeAddresses`
- **Stale snapshot recommendations** (old / orphaned EBS snapshots) —
  `ec2:DescribeSnapshots`, `ec2:DescribeVolumes` (to tell orphaned from live)
- **Idle recommendations** (idle EC2 by utilization) — `ec2:DescribeInstanceStatus`
  + `cloudwatch:GetMetricData` (already listed above for EC2 monitoring)
- **Idle RDS recommendations** (idle DB by connections / CPU) —
  `rds:DescribeDBInstances` + `cloudwatch:GetMetricData` (both already listed above)
- **Idle NAT gateway recommendations** (NAT with ~zero bytes) —
  `ec2:DescribeNatGateways` + `cloudwatch:GetMetricData`
- **Idle load balancer recommendations** (ALB/NLB with no registered targets) —
  `elasticloadbalancing:DescribeLoadBalancers`,
  `elasticloadbalancing:DescribeTargetGroups`,
  `elasticloadbalancing:DescribeTargetHealth` (last two already listed for ELB health)
- **Region discovery / preflight** — `ec2:DescribeRegions`

(`sts:GetCallerIdentity`, used at startup to confirm the assumed identity,
requires no permission grant.)

## Trust policy

The role must trust whatever identity clont runs as. Replace the principal with
your clont runtime role/user ARN.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "AWS": "arn:aws:iam::<CLONT_ACCOUNT>:role/<CLONT_RUNTIME_ROLE>" },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": { "sts:ExternalId": "<optional-external-id>" }
      }
    }
  ]
}
```

- On **EKS**, the runtime role is the pod's IRSA service-account role.
- The `sts:ExternalId` condition is optional — include it only if you set
  `external_id` for that account in `clont.yaml`, and the two must match.
- For **multiple accounts**, repeat this setup in each account; clont assumes
  every configured role independently. An account whose role can't be assumed is
  logged and skipped, not fatal.

## Verifying access

`AWSProvider.preflight()` probes the read-only calls above and returns the
actions that came back `AccessDenied`, so a missing permission can be surfaced
without running a full cycle. (CLI wiring of preflight is pending.)