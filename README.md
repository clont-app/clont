# clont

Monitoring agent — **under active development.**

This package currently reserves the `clont` name on PyPI. It exposes no public
Functionality will land in future releases.

For the product vision, tier model (free local vs paid hosted intelligence), and
roadmap, see [docs/architecture.md](docs/architecture.md).

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

**FinOps** — account-wide spend via Cost Explorer (`GetCostAndUsage`), surfaced as
a daily spend digest (`info`) plus a spend-spike alert (`warn`) when a service's
latest-day cost jumps beyond a configurable % over its trailing baseline.

**Platform**

- **Multi-account** — monitor any number of AWS accounts, keyed by alias; a
  failed account is skipped, not fatal.
- **Read-only by construction** — every call is a `Describe*`/`Get*`; clont never
  writes to your cloud. See [docs/iam.md](docs/iam.md).
- **Refreshable assume-role credentials** — the daemon survives credential
  expiry without restarts.
- **Channels** — log (always on) plus optional Slack, Discord, and Telegram, each
  with its own severity floor and repeat throttling (see below).

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

- `spend_baseline_days` (int, default `7`) — trailing days averaged as the
  baseline a spend spike is measured against.
- `spend_spike_pct` (float, default `50`) — emit a `warn` spike event when a
  service's latest-day spend exceeds the baseline by more than this percentage.
- `spend_min_dollars` (float, default `1`) — ignore services whose latest-day
  spend is below this, so trivial amounts don't trip the spike alert.

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

- Development status: Planning
- License: Apache-2.0