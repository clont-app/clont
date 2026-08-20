# clont

A long-running, strictly **read-only** monitoring and FinOps agent for the cloud.

clont watches your cloud accounts (AWS today) for health issues and cost waste,
emitting `warn`/`critical` events to the channels you configure. Every cloud call
is a `Describe`/`Get`/`List` — the agent never mutates cloud state.

For the product vision, tier model (free local vs paid hosted intelligence), and
architecture, see [docs/architecture.md](docs/architecture.md).

## Features

What's implemented today (AWS, strictly read-only):

**Health monitoring** — per-cycle status checks that emit `warn`/`critical`
events through the pipeline below, each attributed to its account alias and
region:

- **EC2** — instance reachability (`DescribeInstanceStatus`), plus CPU / network
  metrics via CloudWatch `GetMetricData`.
- **RDS** — DB instance status (storage-full / failed / incompatible states).
- **ElastiCache** — cache cluster status.
- **EKS** — cluster status and reported `health.issues`.
- **EBS** — volume status (impaired / insufficient-data).
- **Redshift** — cluster availability.
- **Auto Scaling** — healthy in-service instances vs desired capacity.
- **Load balancers (ALB/NLB)** — target-group health.
- **ECS** — service running-vs-desired count and failed deployments.
- **ACM** — certificate expiry (warn ≤30d, critical ≤7d / expired).
- **AWS Health** — open/upcoming account events (degrades gracefully without a
  Business/Enterprise support plan).
- **Metric anomalies** — CloudWatch metric series (e.g. EC2 CPU / network) are
  watched for statistical deviation: a `warn` fires when the latest sample sits
  more than a configurable number of deviations from its baseline (anomaly
  detection, not fixed thresholds). With enough history the baseline is
  *seasonal* — the latest sample is compared against prior samples at the same
  hour-of-day (robust median/MAD), so a daily load cycle isn't mistaken for an
  anomaly.
- **Capacity & pressure default rules** — opinionated threshold + trend rules over
  native CloudWatch metrics (deterministic, one global default each, operator-tunable):
  - **Predicted disk-full** — a least-squares trend over RDS free storage / Redshift
    disk-used that `warn`s when storage is projected to hit capacity within N days.
  - **Low free storage** (`warn` below 10%, RDS) and **high disk used** (`warn` above
    90%, Redshift).
  - **CPU-credit depletion** — burstable EC2/RDS instances whose `CPUCreditBalance`
    falls below the floor.
  - **Swap pressure** — ElastiCache `SwapUsage` above the threshold.
  *Scope:* "disk-full" covers AWS-native storage metrics (RDS, Redshift). EC2 root /
  EBS *filesystem* fill needs the CloudWatch agent's guest metrics and is not covered
  (clont reads only what AWS exposes without an agent).

**FinOps — spend** — account-wide spend via Cost Explorer (`GetCostAndUsage`),
surfaced as a daily spend digest (`info`) plus a spend-spike alert (`warn`) when a
service's latest-day cost jumps beyond a configurable % over its baseline. The
baseline is the median of prior **same-weekday** spend, so normal weekly cycles
(quiet weekends, busy Mondays) don't trip false spikes.

**FinOps — budgets & forecast** — a month-end spend **forecast** (`info`), a
run-rate projection (month-to-date plus an exponentially-weighted daily rate over
the remaining days), and **budget alerts** against operator-defined monthly
ceilings (whole-account or per-service, set in config): `warn` as the forecast
approaches or is projected to exceed a budget, `critical` once spend has already
breached it. Pure arithmetic — no model, no extra API calls.

**FinOps — recommendations** — read-only savings findings, each carrying a
ballpark monthly dollar figure and emitted through the same event pipeline:

- **Rightsizing** via AWS Compute Optimizer — EC2 instances, EBS volumes, Auto
  Scaling groups, Lambda functions, ECS services and RDS databases, picking the
  best savings option. Each resource type degrades independently when its
  Compute Optimizer opt-in is missing.
- **Commitment purchases** via Cost Explorer — Compute Savings Plans and Reserved
  Instances (EC2 / RDS / ElastiCache / Redshift / OpenSearch), reported at
  conservative one-year, no-upfront terms.
- **Commitment utilization & coverage** via Cost Explorer — Savings Plans / RIs
  you already hold that are under-used (committed spend going to waste) or
  under-covering (eligible on-demand a commitment would discount).
- **Unattached EBS volumes** — `available` volumes still being billed.
- **Unassociated Elastic IPs** — allocated public IPv4 not attached to anything.
- **gp2 → gp3 migration** — in-use gp2 volumes, with the storage-rate saving.
- **Idle EC2 / RDS** — instances or DBs with near-zero utilization (CPU/network,
  or connections/CPU) over a configurable CloudWatch window.
- **Idle NAT gateways** — ~zero bytes processed over the window.
- **Idle load balancers** — ALB/NLB with no registered targets.
- **Stale EBS snapshots** — orphaned (source volume deleted) or older than a
  configurable age.
- **Off-hours scheduling** — always-on non-prod EC2 instances (identified by a
  configurable tag convention) that could be stopped nights and weekends.
- **Tag hygiene** — EC2 instances and EBS volumes missing operator-required tags,
  the root cause of unattributable spend.

Idle/stale thresholds, the non-prod tag convention, and the required-tag list are
all configurable (see `finops.*` below).

**Platform**

- **Multi-account** — monitor any number of AWS accounts, keyed by alias; a
  failed account is skipped, not fatal.
- **Read-only by construction** — every call is a `Describe*`/`Get*`; clont never
  writes to your cloud. See [docs/iam.md](docs/iam.md).
- **Refreshable assume-role credentials** — the daemon survives credential
  expiry without restarts.
- **Channels** — log (always on) plus optional Slack, Discord, and Telegram, each
  with its own severity floor and repeat throttling (see below).
- **API uplink** (optional, paid tier) — with an `api:` block, each cycle ships
  the full batch (metrics, costs, recommendations, health, events) to your clont
  server and dispatches the events it returns through the same channels. Two-way
  by design: channel tokens never leave the agent, so server-side findings ride
  back on the response. Omit the block to stay fully local. See [docs/api.md](docs/api.md).

## How events work

clont runs as a long-running agent. On every cycle it walks the same pipeline:

```
collect (read-only) ──► detect ──► events ──► dispatch to channels
```

1. **Collect.** Read data from each cloud — FinOps cost, monitoring metrics and
   health. The cloud IAM role is strictly read-only; clont never writes to your
   cloud.
2. **Detect.** Turn that data into **events** — an idle resource, a failing
   health check, a spend anomaly. Every event has a **severity**
   (`info` / `warn` / `critical`) and a stable **key** that identifies the
   *condition*, not the occurrence (e.g. `monitoring:health:prod:aws:ec2:i-123`,
   where `prod` is the account alias so two accounts never collide). The key is
   what lets clont recognise the same condition across cycles.
3. **Dispatch.** Hand every event to every channel. Each channel decides on its
   own whether to fire, using two knobs:
   - **`min_severity`** — drop anything below this level.
   - **`repeat_after`** — having fired for a key, stay silent until this much
     time has passed. `none` means "fire once, never repeat".

### How a channel fires

The same firing rule covers every channel; only the defaults differ:

- **log** (always on) — severity floor `info`; re-logs a standing condition
  every ~3h.
- **Slack / Discord / Telegram** — severity floor `warn`; fire once, and
  re-notify only if `repeat_hours` is set.

So a brand-new `critical` health event fires the log **and** every notifier at
once. While the condition persists, the log keeps a throttled record (so it
isn't logged every cycle) and the notifiers stay quiet — unless you've given
them a `repeat_hours` to re-ping for still-open issues. Example timeline for one
condition (log repeat 3h, Slack `repeat_hours=24`, Telegram once-only):

```
t=0h    new       → log ✓  slack ✓  telegram ✓
t=0h05  still open → log –  slack –  telegram –     (within every window)
t=3h    still open → log ✓  slack –  telegram –     (log window elapsed)
t=24h   still open → log ✓  slack ✓  telegram –     (slack re-notifies)
```

All severity and repeat settings are per-channel configuration. Channels live
outside the monitored clouds and use their own credentials (webhook URLs, bot
tokens), kept separate from the read-only cloud role.

### One-shot scan

Channels only tell you what's *wrong*. Once the read-only role is in place, the
quickest way to see what clont actually found is a single cycle with a summary:

```
clont run --summary -                  # one cycle, print the summary, exit
clont run --summary scan.txt           # shareable report (see below)
clont run --summary scan.json          # machine-readable
clont run --summary out --format json  # extension picks the format; this overrides
```

Three formats, three audiences:

| Format | From | For |
|---|---|---|
| `text` | `-`, any other extension | you, right after the run — terse counts |
| `report` | `.txt` | the person you send it to |
| `json` | `.json` | scripts, dashboards |

The summary reports the accounts scanned, how much was collected (metrics,
costs, recommendations, health checks), events broken down by severity and
domain, the non-`ok` health checks, estimated monthly savings (per currency),
the top services by spend, and any collector that failed — so an empty result
can be told apart from a role that couldn't read anything.

`--summary` runs exactly one cycle, so events still reach the channels as usual.
Add `--fail-on-critical` to exit `2` when the cycle produced a critical event,
which makes the command usable as a CI gate.

### The shareable report

`.txt` gets a report meant to be sent to someone who wasn't there — it leads
with the headline number and puts the evidence under it:

```
====================================================================
 CLOUD WASTE REPORT
====================================================================

  you're wasting
    $1,229.75 / month
    $14,757.00 / year

  12 finding(s) across 2 account(s): prod, staging
  scanned 2026-08-20 11:04 UTC  |  clont 0.2.0  |  4.216s

--------------------------------------------------------------------
 WHERE THE MONEY GOES
--------------------------------------------------------------------

  $412.80/mo   x1    idle_rds (rds)
                 - db-analytics-old [eu-west-1]  $412.80/mo  0 connections for 21d

  $388.80/mo   x4    idle_nat (ec2)
                 - nat-0a1b2c3d [eu-west-1]  $97.20/mo  no traffic for 30d
                 ... and 3 more
```

Findings are grouped by kind and sorted by money, biggest first, with the top
resources named under each. Then top spend, health, and any collector errors.

Two things it will not do: sum across currencies, or print an all-clear it
can't back. If collectors failed, the report says the figure is a floor and
lists the failures; if nothing was configured, it says that instead.

## Configuration

clont is configured by a single YAML file (see `clont.example.yaml` for the full
reference). Point `$CLONT_CONFIG` at it, or drop a `clont.yaml` in the working
directory; `clont run --config <path>` overrides both. In Kubernetes the file is
a mounted ConfigMap.

- On startup the file is **validated** (pydantic) — bad or unknown keys fail
  fast with a clear error.
- If no config file exists, clont **writes one with defaults** at the resolved
  path on first run, then continues.
- Read-only cloud access (the IAM `role_arn`) is declared in the config, not on
  the command line. See [docs/iam.md](docs/iam.md) for the role's read-only
  permissions and trust policy.

The YAML file is the single source of truth — individual fields are not
overridable by environment variables. The only env var clont reads is
`CLONT_CONFIG`, which just points at the file.

### Parameters

**Top level**

- `interval_seconds` (int, default `300`) — how often the agent runs a cycle.
- `lookback_days` (int, default `1`) — window for cost / metric queries.
- `log_level` (enum, default `info`) — the daemon's own operational log
  verbosity: `debug` / `info` / `warning` / `error` / `critical`. Distinct from
  `channels.log.min_severity`, which gates which detected *events* are logged.
- `aws` (map, default `{}`) — read-only AWS accounts to monitor, keyed by alias
  (see below).
- `channels` (object, default log only) — outbound delivery channels (see below).

**`aws.<alias>`** — one entry per account; the alias (the map key, e.g. `prod`,
`staging`) is shown in notifications and used to attribute events, so two
accounts never collide. Add a second account by adding another keyed entry.

- `role_arn` (str, **required**) — read-only IAM role clont assumes (via IRSA on EKS).
- `regions` (list of str, default `[]`) — regions to query.
- `external_id` (str, default `null`) — optional STS external id for the assume-role.

If one account's role can't be assumed at startup, clont logs a warning and
keeps monitoring the rest; it aborts only if no account authenticates.

**`finops`** — Cost Explorer spend-event thresholds

- `spend_baseline_days` (int, default `28`) — trailing window the spike baseline
  is built from. ~4 weeks gives several same-weekday samples; the baseline is the
  median of prior same-weekday spend (falls back to the flat mean on short
  windows).
- `spend_spike_pct` (float, default `50`) — emit a `warn` spike event when a
  service's latest-day spend exceeds the baseline by more than this percentage.
- `spend_min_dollars` (float, default `1`) — ignore services whose latest-day
  spend is below this, so trivial amounts don't trip the spike alert.
- `budgets` (list, default `[]`) — monthly spend ceilings. Each entry has
  `monthly_limit` (required), `account` (alias, or `"*"` for every account,
  default `"*"`), optional `service` (a Cost Explorer service name; omit for a
  whole-account budget), and `currency` (default `USD`).
- `budget_warn_pct` (float, default `80`) — emit a `warn` when the month-end
  forecast reaches this percentage of a budget (a `critical` fires once spend has
  actually breached it).
- `forecast_alpha` (float, default `0.5`) — EWMA recency weight for the
  daily-rate month-end forecast (higher = more weight on recent days).
- `idle_cpu_pct` (float, default `5`) — average CPU % below which an EC2/RDS
  resource counts as idle.
- `idle_lookback_days` (int, default `14`) — trailing window the idle averages
  (CPU / network / connections / NAT bytes) are taken over.
- `idle_rds_max_connections` (float, default `1`) — average DB connections below
  which an RDS instance counts as idle.
- `snapshot_max_age_days` (int, default `90`) — EBS snapshots older than this are
  flagged as "old".
- `ri_sp_min_utilization` (float, default `90`) — flag a Savings Plan / Reserved
  Instance used below this percentage (paying for unused commitment).
- `ri_sp_min_coverage` (float, default `70`) — flag when eligible usage is covered
  below this percentage (on-demand spend a commitment would discount).
- `nonprod_tags` (map of tag key → values, default `{}`) — tags marking
  schedulable non-prod resources, e.g. `Environment: [dev, staging, test, qa]`.
  Empty disables the off-hours collector (it never guesses which boxes are non-prod).
- `required_tags` (list of str, default `[]`) — tag keys every cost-bearing
  resource must carry; empty disables the tag-hygiene collector.

**`monitoring`** — metric-anomaly detection

- `anomaly_sigma` (float, default `3`) — emit a `warn` anomaly when the latest
  metric sample is more than this many standard deviations from its baseline.
- `anomaly_min_points` (int, default `6`) — minimum baseline samples a series
  needs before it can flag an anomaly.
- `free_storage_min_pct` (float, default `10`) — `warn` when RDS free storage drops
  below this percentage.
- `disk_used_max_pct` (float, default `90`) — `warn` when Redshift disk used rises
  above this percentage.
- `cpu_credit_min_balance` (float, default `20`) — `warn` when a burstable EC2/RDS
  `CPUCreditBalance` falls below this.
- `swap_usage_max_mb` (float, default `50`) — `warn` when ElastiCache swap usage
  exceeds this (MB).
- `disk_full_forecast_days` (float, default `14`) — `warn` when the storage trend is
  projected to hit capacity within this many days.

**`channels.log`** — always on

- `repeat_hours` (float, default `3.0`) — re-log a standing condition at most this often.
- `min_severity` (enum, default `info`) — drop events below this level.

**`channels.slack` / `channels.discord` / `channels.telegram`** — all optional

- `webhook_url` (str, **required** for slack & discord) — incoming webhook URL.
- `bot_token` (str, **required** for telegram) — bot token from @BotFather.
- `chat_id` (str, **required** for telegram) — target chat / channel / group id.
- `min_severity` (enum, default `warn`) — drop events below this level.
- `repeat_hours` (float, default `null`) — `null` notifies once per condition; a
  value re-notifies a still-open one that often.

`min_severity` accepts `info`, `warn`, or `critical`.

## Status

- Development status: Active
- License: Apache-2.0