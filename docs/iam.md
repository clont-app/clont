# read-only iam setup

clont only ever **reads** from your aws accounts. for each account you watch,
make one iam role that clont assumes, with a read-only permissions policy and a
trust policy that lets clont's runtime identity in. the role arn goes in
`clont.yaml` under the account's alias (see `clont.example.yaml`).

```
runtime identity  ──sts:AssumeRole──►  clont-readonly role (per account)
(irsa on eks, or                       read-only permissions below
 an iam user/role locally)
```

## the permissions policy

attach this to the `clont-readonly` role. none of these actions support
resource-level scoping, so `Resource` is `*`. they're all read-only.

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
        "ec2:DescribeAddresses",
        "ec2:DescribeNetworkInterfaces"
      ],
      "Resource": "*"
    }
  ]
}
```

all of that is free. the list grows as you turn on more collectors.

> upgrading? `ec2:DescribeNetworkInterfaces` is new — it's what the public ipv4
> collector uses. without it that collector is skipped and you keep everything
> else.

## the two billed grants — only if you want them

both are **deliberately missing from the policy above**. neither is needed to run
clont. each buys you something specific and each has a meter running.

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

| knob | grant | meter | what it costs |
|---|---|---|---|
| `finops.allow_cost_explorer` | `ce:GetCostAndUsage` | $0.01 a request | one request per refresh — ~$0.30/mo per account at the default daily cadence, ~$88/mo if you drop `collect_interval_seconds` to the 300s loop |
| `monitoring.metrics.enabled` | `cloudwatch:GetMetricData` | $0.01 per 1,000 metrics asked for | grows with the **fleet**, not the cycle: `max_metrics_per_cycle` caps one cycle, `collect_every_seconds` caps the day. 1,000 metrics every 300s ≈ $86/mo per account |
| `finops.allow_cloudwatch_metrics` | `cloudwatch:GetMetricData` | same | one metric per ec2 / rds / nat resource per refresh. compute optimizer answers the same question for free — only turn this on for an account that isn't enrolled |

two things worth remembering:

- **you pay for cadence, not for the loop.** `interval_seconds` (300) is how
  often clont wakes up. `finops.collect_interval_seconds` (86400) and
  `monitoring.metrics.collect_every_seconds` are how often it actually calls out.
  the cached result still feeds the detectors every cycle, so a shorter cadence
  buys freshness, not coverage. `clont run --summary` always forces a full
  refresh, so an ad-hoc scan sees today's numbers.
- **the default setup makes no billed api call at all.** spend comes from the cur
  (a couple of s3 gets), idle advice from compute optimizer, commitment advice
  from free describes, and public ipv4 from two free ec2 describes.

## what each collector needs

- **spend** (daily account cost) — `s3:GetObject` on the cost and usage report,
  granted separately (below). cost explorer's `ce:GetCostAndUsage` is **not** in
  the policy: it bills $0.01 a request and the cur has the same numbers for free.
  add it only if you set `finops.allow_cost_explorer`.
- **commitment recommendations** (savings plans + reserved instances) and
  **utilization & coverage** (commitments you own that are under-used or
  under-covering) — `ec2:DescribeInstances`, `ec2:DescribeReservedInstances`,
  `savingsplans:DescribeSavingsPlans`. all free; cost explorer's billed
  `Get*Recommendation` / `Get*Utilization` / `Get*Coverage` calls aren't used any
  more. `savingsplans:DescribeSavingsPlans` is the grant most existing roles are
  missing — without it the savings plans half is skipped and the ri half still
  reports. these come from a snapshot of current usage, not cost explorer's 30-day
  lookback, so they won't match the console exactly.
- **budgets + month-end forecast** — no extra grant, it reuses the spend stream.
- **ec2 health** (reachability) — `ec2:DescribeInstanceStatus`
- **ec2 metrics** (cpu / network) — `cloudwatch:GetMetricData`, **billed and off
  by default** (`monitoring.metrics.enabled`); instances are found via
  `ec2:DescribeInstanceStatus`
- **rds health** — `rds:DescribeDBInstances`
- **elasticache health** — `elasticache:DescribeCacheClusters`
- **eks health** — `eks:ListClusters`, `eks:DescribeCluster`
- **ebs health** — `ec2:DescribeVolumeStatus`
- **redshift health** — `redshift:DescribeClusters`
- **auto scaling health** — `autoscaling:DescribeAutoScalingGroups`
- **load balancer health** — `elasticloadbalancing:DescribeTargetGroups`,
  `elasticloadbalancing:DescribeTargetHealth`
- **ecs health** — `ecs:ListClusters`, `ecs:ListServices`, `ecs:DescribeServices`
- **acm expiry** — `acm:ListCertificates`, `acm:DescribeCertificate`
- **aws health** (account events) — `health:DescribeEvents` (needs a business or
  enterprise support plan; denied gracefully without one)
- **compute optimizer rightsizing** (ec2 / ebs / auto scaling / lambda / ecs / rds) —
  `compute-optimizer:GetEC2InstanceRecommendations`,
  `compute-optimizer:GetEBSVolumeRecommendations`,
  `compute-optimizer:GetAutoScalingGroupRecommendations`,
  `compute-optimizer:GetLambdaFunctionRecommendations`,
  `compute-optimizer:GetECSServiceRecommendations`,
  `compute-optimizer:GetRDSDatabaseRecommendations` (each resource type is opted
  into separately; one that isn't enrolled is skipped without hurting the others)
- **idle recommendations** (idle ec2, asgs, ebs, ecs services, rds and nat
  gateways with their monthly saving) — `compute-optimizer:GetIdleRecommendations`
  plus `compute-optimizer:GetEnrollmentStatus` for the startup probe that tells
  "not enrolled" apart from "nothing idle". free, and it replaces the metric-based
  detectors further down.
- **waste recommendations** (unattached ebs, gp2→gp3) — `ec2:DescribeVolumes`
- **public ipv4** (what every billable address costs you, plus the wasted ones) —
  `ec2:DescribeNetworkInterfaces`, `ec2:DescribeAddresses`. two free describes per
  region. every billable address hangs off a network interface, and the addresses
  call catches the unassociated elastic ips that don't have one. idle elastic ips
  used to be reported by the waste collector; they live here now, alongside
  addresses on detached interfaces and secondary addresses billed on top of a
  primary.
- **stale snapshots** (old or orphaned) — `ec2:DescribeSnapshots`,
  `ec2:DescribeVolumes` (to tell orphaned from live)
- **metric-based idle detectors** (idle ec2 by cpu, idle rds by connections, nat
  with almost no traffic) — off unless `finops.allow_cloudwatch_metrics` is set,
  for accounts not enrolled in compute optimizer. then:
  `ec2:DescribeInstanceStatus`, `rds:DescribeDBInstances`,
  `ec2:DescribeNatGateways` + `cloudwatch:GetMetricData` (one metric per resource
  per cycle — this is the grant whose bill grows with your fleet)
- **idle load balancers** (nothing registered) —
  `elasticloadbalancing:DescribeLoadBalancers`,
  `elasticloadbalancing:DescribeTargetGroups`,
  `elasticloadbalancing:DescribeTargetHealth` (last two already listed for health)
- **off-hours scheduling** — `ec2:DescribeInstances` (state + tags; needs
  `nonprod_tags`)
- **tag hygiene** — `ec2:DescribeInstances`, `ec2:DescribeVolumes` (tags; needs
  `required_tags`)
- **monitoring default rules** (disk-full forecast, low free storage, cpu credits,
  swap) — the same billed `cloudwatch:GetMetricData` as ec2 metrics, so they're
  inert until `monitoring.metrics.enabled`. reads `AWS/RDS` (`FreeStorageSpace`,
  `CPUCreditBalance`), `AWS/Redshift` (`PercentageDiskSpaceUsed`),
  `AWS/ElastiCache` (`SwapUsage`, `FreeableMemory`) and `AWS/EC2`
  (`CPUCreditBalance`), with resources found through the describes already listed.
- **region discovery / preflight** — `ec2:DescribeRegions`. every preflight probe
  is free.

(`sts:GetCallerIdentity`, used at startup to confirm who clont assumed, needs no
grant.)

## spend: the cost and usage report

spend comes from the cur your account already writes to s3, so a cycle costs a
couple of s3 gets instead of a billed cost explorer request. create a **legacy
cur** (gzip + csv, hourly or daily) delivered to a bucket the role can read, then
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

give the role read on that one report. clont works out the manifest key from the
billing period and never lists the bucket, so no `s3:ListBucket` needed:

```json
{
  "Sid": "ClontCUR",
  "Effect": "Allow",
  "Action": "s3:GetObject",
  "Resource": "arn:aws:s3:::my-billing-bucket/reports/clont-cur/*"
}
```

stuff that will bite you:

- **service names aren't spelled like cost explorer.** clont reports the cur's
  `product/ProductName` (`Amazon Elastic Compute Cloud`, not
  `Amazon Elastic Compute Cloud - Compute`), so budgets keyed by service need the
  cur spelling.
- **a payer's report covers every linked account.** by default clont keeps only
  the rows matching the account it authenticated as, so each alias reports its
  own spend. set `include_linked: true` to take the whole thing.
- **the report is rewritten a few times a day**, so it's re-read at most every
  `refresh_minutes` (default 60), not every cycle.
- **a brand new report can take 24h to appear.** until then spend is empty and
  preflight says so.
- without `cur` and without `finops.allow_cost_explorer: true` there's no spend
  data at all — recommendations and health still work fine.

## trust policy

the role has to trust whatever identity clont runs as. swap the principal for
your clont runtime role/user arn.

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

- on **eks**, the runtime role is the pod's irsa service-account role.
- the `sts:ExternalId` condition is optional — add it only if you set
  `external_id` for that account in `clont.yaml`, and the two must match.
- for **several accounts**, repeat this in each one. clont assumes every
  configured role on its own; an account it can't get into is logged and skipped,
  not fatal.

## checking access

`AWSProvider.preflight()` probes the read-only calls above and hands back the
ones that came home `AccessDenied`, so you can find a missing permission without
running a whole cycle. (cli wiring for it is still pending.)
