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
        "ec2:DescribeRegions",
        "ec2:DescribeInstances",
        "ec2:DescribeReservedInstances",
        "savingsplans:DescribeSavingsPlans",
        "ec2:DescribeInstanceStatus",
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
        "compute-optimizer:GetIdleRecommendations",
        "compute-optimizer:GetEnrollmentStatus",
        "ec2:DescribeVolumes",
        "ec2:DescribeAddresses"
      ],
      "Resource": "*"
    }
  ]
}
```

Everything above is free. The action list grows as you enable more collectors.

## Only if you opt in — the two billed grants

Both are **absent from the policy above on purpose**. Neither is needed to run
clont; each buys something specific, and each has a meter attached.

```json
{
  "Sid": "ClontBilled",
  "Effect": "Allow",
  "Action": [
    "ce:GetCostAndUsage",
    "cloudwatch:GetMetricData"
  ],
  "Resource": "*"
}
```

| Knob | Grant it needs | Meter | What it costs |
|---|---|---|---|
| `finops.allow_cost_explorer` | `ce:GetCostAndUsage` | $0.01 per request | one request per refresh — ~$0.30/mo per account at the default daily cadence, ~$88/mo if you drop `collect_interval_seconds` to the 300s loop interval |
| `monitoring.metrics.enabled` | `cloudwatch:GetMetricData` | $0.01 per 1,000 metrics requested | scales with the **fleet**, not the cycle: `max_metrics_per_cycle` caps one cycle, `collect_every_seconds` caps the day. 1,000 metrics every cycle at 300s ≈ $86/mo per account |
| `finops.allow_cloudwatch_metrics` | `cloudwatch:GetMetricData` | same | one metric per EC2 / RDS / NAT resource per refresh. Compute Optimizer answers the same question free — turn this on only for an account not enrolled in CO |

Two things worth internalising:

- **Cadence, not the loop interval, is what you pay for.** `interval_seconds`
  (300) is how often clont wakes up; `finops.collect_interval_seconds` (86400)
  and `monitoring.metrics.collect_every_seconds` are how often it actually calls
  out. The cached result is still fed to the detectors every cycle, so lowering
  a cadence buys freshness, not coverage. `clont run --summary` always forces a
  full refresh, so an ad-hoc scan sees today's numbers.
- **The default configuration makes no billed API call at all.** Spend comes
  from the CUR (a couple of S3 GETs), idle advice from Compute Optimizer, and
  commitment advice from free describes.

## What each collector needs

- **Spend** (account-wide daily cost) — `s3:GetObject` on the Cost and Usage
  Report, granted separately (see below). Cost Explorer's `ce:GetCostAndUsage`
  is **not** in the policy: it bills $0.01 per request and the CUR carries the
  same numbers for free. Add it only if you set `finops.allow_cost_explorer`.
- **Commitment recommendations** (Savings Plans + Reserved Instances) and
  **commitment utilization & coverage** (under-used or under-covering SP/RIs) —
  `ec2:DescribeInstances`, `ec2:DescribeReservedInstances`,
  `savingsplans:DescribeSavingsPlans`. All free; Cost Explorer's billed
  `Get*Recommendation` / `Get*Utilization` / `Get*Coverage` calls are no longer
  used. `savingsplans:DescribeSavingsPlans` is the one grant most existing roles
  lack — without it the Savings Plans half is skipped and the RI half still
  reports. Note these figures come from a snapshot of current usage, not Cost
  Explorer's 30-day lookback, so they won't match the console exactly.
- **Budgets + month-end forecast** (run-rate projection vs operator budgets) —
  no extra grant; reuses the daily spend stream
- **EC2 health** (instance reachability) — `ec2:DescribeInstanceStatus`
- **EC2 metrics** (CPU / network) — `cloudwatch:GetMetricData`, **billed and off by
  default** (`monitoring.metrics.enabled`); instances are discovered via
  `ec2:DescribeInstanceStatus`
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
- **Idle recommendations** (idle/unattached/unused EC2, Auto Scaling groups, EBS
  volumes, ECS services, RDS databases and NAT gateways, with the monthly saving) —
  `compute-optimizer:GetIdleRecommendations`, plus
  `compute-optimizer:GetEnrollmentStatus` for the startup probe that tells
  "not enrolled" apart from "nothing idle". Free, and it replaces the metric-based
  idle detectors below.
- **Waste recommendations** (unattached EBS, unassociated Elastic IPs, gp2→gp3) —
  `ec2:DescribeVolumes`, `ec2:DescribeAddresses`
- **Stale snapshot recommendations** (old / orphaned EBS snapshots) —
  `ec2:DescribeSnapshots`, `ec2:DescribeVolumes` (to tell orphaned from live)
- **Metric-based idle detectors** (idle EC2 by utilization, idle RDS by
  connections / CPU, NAT with ~zero bytes) — off unless
  `finops.allow_cloudwatch_metrics` is set, for accounts not enrolled in Compute
  Optimizer. Then: `ec2:DescribeInstanceStatus`, `rds:DescribeDBInstances`,
  `ec2:DescribeNatGateways` + `cloudwatch:GetMetricData` (one metric per resource
  per cycle — this is the grant whose bill grows with the fleet)
- **Idle load balancer recommendations** (ALB/NLB with no registered targets) —
  `elasticloadbalancing:DescribeLoadBalancers`,
  `elasticloadbalancing:DescribeTargetGroups`,
  `elasticloadbalancing:DescribeTargetHealth` (last two already listed for ELB health)
- **Off-hours scheduling recommendations** (always-on non-prod instances) —
  `ec2:DescribeInstances` (reads instance state + tags; gated on `nonprod_tags`)
- **Tag-hygiene recommendations** (resources missing required tags) —
  `ec2:DescribeInstances`, `ec2:DescribeVolumes` (reads tags; gated on `required_tags`)
- **Monitoring default rules** (disk-full forecast, low free storage, CPU-credit
  depletion, swap pressure) — the same billed `cloudwatch:GetMetricData` as EC2
  metrics, so they too are inert until `monitoring.metrics.enabled`; it reads the
  `AWS/RDS` (`FreeStorageSpace`, `CPUCreditBalance`), `AWS/Redshift`
  (`PercentageDiskSpaceUsed`), `AWS/ElastiCache` (`SwapUsage`, `FreeableMemory`) and
  `AWS/EC2` (`CPUCreditBalance`) namespaces, with resources discovered via the
  already-listed `rds:DescribeDBInstances` / `redshift:DescribeClusters` /
  `elasticache:DescribeCacheClusters` / `ec2:DescribeInstanceStatus`
- **Region discovery / preflight** — `ec2:DescribeRegions`. Every preflight probe
  is free.

(`sts:GetCallerIdentity`, used at startup to confirm the assumed identity,
requires no permission grant.)

## Spend: the Cost and Usage Report

Spend comes from the CUR the account already writes to S3, so a cycle costs a
couple of S3 GETs instead of a billed Cost Explorer request. Create a **legacy
CUR** (gzip + csv, hourly or daily) delivered to a bucket the role can read, then
point clont at it:

```yaml
aws:
  prod:
    role_arn: arn:aws:iam::111111111111:role/clont-readonly
    cur:
      bucket: my-billing-bucket
      report_name: clont-cur      # the report name = its folder in the bucket
      prefix: reports             # the s3 prefix you gave the report
      region: us-east-1           # bucket region
```

Grant the role read on that report only — clont derives the manifest key from the
billing period and never lists the bucket, so `s3:ListBucket` isn't needed:

```json
{
  "Sid": "ClontCUR",
  "Effect": "Allow",
  "Action": "s3:GetObject",
  "Resource": "arn:aws:s3:::my-billing-bucket/reports/clont-cur/*"
}
```

Things worth knowing:

- **Service names differ slightly from Cost Explorer.** The CUR's
  `product/ProductName` is what clont reports (`Amazon Elastic Compute Cloud`,
  not `Amazon Elastic Compute Cloud - Compute`), so budgets keyed by service
  need the CUR spelling.
- **A payer's report covers every linked account.** By default clont keeps only
  the rows whose usage account matches the account it authenticated as, so each
  alias reports its own spend. Set `include_linked: true` to take the whole
  report instead.
- **The report is rewritten a few times a day**, so it is re-read at most every
  `refresh_minutes` (default 60), not every cycle.
- **A fresh report takes up to 24h to show up.** Until then spend is empty and
  preflight says so.
- Without `cur`, and without `finops.allow_cost_explorer: true`, there is no
  spend data at all — recommendations and health still work.

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