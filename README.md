# clont

a long-running, **read-only** agent that watches your cloud for broken things and
wasted money.

clont sits next to your aws accounts, looks around every few minutes, and pings
you on slack / discord / telegram when something is wrong or costing you more
than it should. every call it makes is a `describe` / `get` / `list` — it never
changes anything in your cloud.

for the bigger picture (free local tier vs paid hosted one, how the pieces fit)
see [docs/architecture.md](docs/architecture.md).

## what it watches

### health

per-cycle checks, each tagged with the account alias and region it came from:

- **ec2** — instance reachability, plus cpu / network metrics if you turn on
  `monitoring.metrics.enabled` (that one is billed, so it's off by default).
- **rds** — db instance status (storage full, failed, incompatible).
- **elasticache** — cache cluster status.
- **eks** — cluster status and whatever `health.issues` reports.
- **ebs** — volume status (impaired, insufficient-data).
- **redshift** — cluster availability.
- **auto scaling** — in-service instances vs what you asked for.
- **load balancers** — alb / nlb target group health.
- **ecs** — running vs desired count, failed deployments.
- **acm** — certs about to expire (warn at 30 days, critical at 7 or already gone).
- **aws health** — open and upcoming account events. works fine without a
  business support plan, it just gets skipped.
- **metric anomalies** — instead of fixed thresholds, clont compares the newest
  sample against its own baseline and warns when it drifts too far. with enough
  history the baseline is *seasonal*: 9am is compared to other 9ams, so your
  normal daily traffic curve doesn't page you.
- **capacity rules** — a few opinionated defaults on top of native cloudwatch
  metrics:
  - **disk filling up** — a trend line over rds free storage / redshift disk used,
    warns if it's going to hit the wall in the next n days.
  - **low free storage** (rds, under 10%) and **high disk used** (redshift, over 90%).
  - **cpu credits running out** on burstable ec2 / rds.
  - **swap pressure** on elasticache.

  heads up: "disk full" only covers what aws exposes itself (rds, redshift).
  filesystem usage inside an ec2 box needs the cloudwatch agent, and clont only
  reads what's there without one.

### spend

daily account spend comes from the **cost and usage report** your account already
writes to s3. that's free — cost explorer charges $0.01 a request and the agent
would hit it every cycle.

you get a daily spend digest (`info`) plus a spike alert (`warn`) when a service
jumps past its baseline by more than you allow. the baseline is the median of
previous **same-weekday** spend, so quiet weekends and busy mondays don't look
like spikes.

### budgets and forecast

a month-end **forecast** (`info`) from month-to-date plus a weighted daily rate,
and **budget alerts** against ceilings you set: `warn` when the forecast gets
close or is heading over, `critical` once you've actually blown through it. it's
plain arithmetic — no model, no extra api calls.

### savings findings

read-only recommendations, each with a rough monthly dollar figure, sent through
the same event pipeline:

- **rightsizing** via compute optimizer — ec2, ebs, auto scaling groups, lambda,
  ecs and rds, picking the best savings option. each resource type degrades on
  its own if you haven't opted it in.
- **commitments to buy** — compute savings plans and ec2 reserved instances, at
  the conservative one-year no-upfront terms.
- **commitments you already own** that are under-used (paying for headroom you
  don't touch) or under-covering (on-demand a commitment would discount).

  both come from what the account actually runs right now (`DescribeInstances`,
  `DescribeReservedInstances`, `DescribeSavingsPlans` — all free) instead of cost
  explorer's billed recommendation apis, which would be 10 requests a cycle. the
  trade: it's a snapshot, not a 30-day average, so the numbers won't match the
  console. and only 70% of uncovered spend ever gets advised as a commitment, so
  one busy afternoon can't talk you into a year-long contract.
- **unattached ebs volumes** — `available` volumes you're still paying for.
- **public ipv4 addresses** — see below, it's the newest one.
- **gp2 → gp3** — in-use gp2 volumes, with the storage-rate saving.
- **idle resources** — ec2, auto scaling groups, ebs, ecs services, rds and nat
  gateways that compute optimizer calls idle, each with its monthly saving. free,
  and it replaced the metric-based detectors that could only show you the
  evidence. those are still around as an opt-in fallback
  (`finops.allow_cloudwatch_metrics`) if an account isn't enrolled.
- **idle load balancers** — alb / nlb with nothing registered behind them.
- **stale ebs snapshots** — orphaned (source volume gone) or just old.
- **s3 storage waste** — buckets with no lifecycle rule, versioning that keeps
  every overwrite forever, and incomplete multipart uploads: parts that never
  finished, billed as storage and invisible in the console. a rule scoped to one
  prefix does not count as covering the bucket. with
  `finops.allow_cloudwatch_metrics` it also sizes what sits in Standard with
  nothing moving it anywhere cheaper — a candidate, not a finding, because read
  frequency needs s3 request metrics and those are billed.
- **off-hours scheduling** — always-on non-prod instances (you pick the tag
  convention) that could sleep at night and on weekends.
- **tag hygiene** — ec2, ebs, rds, load balancers, lambda and s3 buckets missing
  tags you require, which is where unattributable spend comes from.
- **showback by tag** — the same keys turned into spend per team / cost centre,
  with the unattributed share reported as its own line. that share is the number
  that justifies fixing the tags. grouping is cloud-agnostic: whatever fills a
  cost record's tags gets the report.
- **data transfer** — network spend split into cross-az, inter-region, internet
  egress, nat processing, cdn and privatelink, with the top talking service in
  each. it is normally 5-15% of a bill and has no api of its own; the usage type
  in cur is the only free place that says which transfer you bought.
- **nat paying for free traffic** — a vpc with a nat gateway and no s3/dynamodb
  gateway endpoint pays per gigabyte for traffic the endpoint carries for
  nothing. the classic transfer finding, and two free describes to catch.

thresholds, the non-prod tag convention and the required-tag list are all
configurable under `finops.*`.

## public ipv4 — every address is a bill now

since february 2024 aws charges **$0.005/hr for every public ipv4 address**,
attached or not. that's about $3.65 a month each, and nobody notices because it
never shows up as its own line item on anything you look at.

so clont counts them. all of them.

**how it finds them.** every billable address in a vpc hangs off a network
interface, so one paginated `DescribeNetworkInterfaces` per region sees nat
gateways, load balancers, instances, rds and fargate in one shot. a second call,
`DescribeAddresses`, picks up elastic ips that aren't attached to anything and so
have no interface. two free describes per region — no cost explorer, no
cloudwatch, no price lookup at runtime. **running this collector costs you $0.**

**what it reports.** a daily cost record per region with how many addresses you
have and what they're costing, stamped per-day like every other spend source so
it lands in the daily digest and the spike detector sees it properly. addresses
are deduped by ip (an attached elastic ip shows up in both calls) and byoip
addresses are dropped, since aws doesn't charge you for those.

**what it recommends.** counting is not blaming. a public ip on your production
load balancer is a cost, not a mistake, and telling you to "save $3.65" on it
would just teach you to ignore clont. so only the clear waste turns into a
recommendation:

| finding | why it's waste |
|---|---|
| unassociated elastic ip | allocated, attached to nothing, billed anyway |
| address on a detached interface | the thing behind it is gone, the ip isn't |
| secondary public ip | billed on top of the primary on the same interface |

each one is its own event, so two secondaries on one interface don't collapse
into a single alert.

the extra iam you need is one action: `ec2:DescribeNetworkInterfaces`.

## platform

- **many accounts** — as many as you like, keyed by an alias you choose. one
  account failing gets skipped, not fatal. on an org, point clont at the payer
  and set `members.role_name`: it lists the linked accounts itself and splits the
  payer's spend per account, so each one gets its own digest, spike and budget.
- **read-only by construction** — every call is a `Describe*` / `Get*`. see
  [docs/iam.md](docs/iam.md), which also lists the two billed grants
  (`ce:GetCostAndUsage`, `cloudwatch:GetMetricData`) that are left out of the
  policy on purpose, and what each costs if you want them. out of the box clont
  makes **no billed api call at all**.
- **credentials refresh themselves** — the agent survives expiry without a restart.
- **channels** — log (always on) plus slack, discord and telegram, each with its
  own severity floor and repeat throttle.
- **api uplink** (optional, paid) — add an `api:` block and each cycle ships its
  batch to your clont server and dispatches whatever events come back. two-way on
  purpose: your channel tokens never leave the agent. leave the block out and
  everything stays local. see [docs/api.md](docs/api.md).

## how events work

every cycle walks the same path:

```
collect (read-only) ──► detect ──► events ──► send to channels
```

1. **collect.** read cost, metrics and health from each cloud.
2. **detect.** turn that into **events** — an idle resource, a failing check, a
   spend spike. each event has a severity (`info` / `warn` / `critical`) and a
   stable **key** naming the *condition*, not the moment
   (`monitoring:health:prod:aws:ec2:i-123` — `prod` is the account alias, so two
   accounts never collide). the key is how clont recognises the same problem next
   cycle.
3. **dispatch.** hand every event to every channel. each channel decides for
   itself with two knobs:
   - **`min_severity`** — ignore anything below this.
   - **`repeat_after`** — after firing for a key, stay quiet this long. `none`
     means fire once and never again.

### when a channel actually fires

same rule everywhere, only the defaults differ:

- **log** (always on) — floor `info`, re-logs a standing problem every ~3h.
- **slack / discord / telegram** — floor `warn`, fire once, repeat only if you
  set `repeat_hours`.

so a brand new `critical` hits the log and every notifier at once. while it's
still open the log keeps a throttled record and the notifiers stay quiet, unless
you gave them a `repeat_hours` to nag you. one condition, log repeat 3h, slack
`repeat_hours=24`, telegram once-only:

```
t=0h    new        → log ✓  slack ✓  telegram ✓
t=0h05  still open → log –  slack –  telegram –     (everyone's in their window)
t=3h    still open → log ✓  slack –  telegram –     (log window elapsed)
t=24h   still open → log ✓  slack ✓  telegram –     (slack nags again)
```

channels live outside the clouds they report on and use their own credentials,
kept away from the read-only cloud role.

## one-shot scan

channels only tell you what's wrong. once the read-only role is in place, the
fastest way to see what clont found is a single cycle with a summary:

```
clont run --summary -                  # one cycle, print it, exit
clont run --summary scan.txt           # a report you can send someone
clont run --summary scan.json          # for scripts
clont run --summary out --format json  # extension picks the format; this wins
```

| format | from | for |
|---|---|---|
| `text` | `-`, any other extension | you, right after the run — just counts |
| `report` | `.txt` | the person you're sending it to |
| `json` | `.json` | scripts, dashboards |

the summary lists accounts scanned, how much was collected, events by severity
and domain, the non-`ok` health checks, estimated monthly savings per currency,
top services by spend, and any collector that failed — so "nothing found" can be
told apart from "the role couldn't read anything".

`--summary` runs exactly one real cycle, so events still reach your channels. add
`--fail-on-critical` to exit `2` on a critical, which makes it usable as a ci gate.

### the shareable report

`.txt` gives you something meant for a person who wasn't there. big number first,
evidence under it:

```
====================================================================
 CLOUD WASTE REPORT
====================================================================

  you're wasting
    $1,229.75 / month
    $14,757.00 / year

  12 finding(s) across 2 account(s): prod, staging
  scanned 2026-08-20 11:04 UTC  |  clont 0.2.2  |  4.216s

--------------------------------------------------------------------
 WHERE THE MONEY GOES
--------------------------------------------------------------------

  $412.80/mo   x1    idle_rds (rds)
                 - db-analytics-old [eu-west-1]  $412.80/mo  0 connections for 21d

  $388.80/mo   x4    idle_nat (ec2)
                 - nat-0a1b2c3d [eu-west-1]  $97.20/mo  no traffic for 30d
                 ... and 3 more
```

findings are grouped by kind and sorted by money, biggest first. then top spend,
health, and any collector errors.

two things it won't do: add up different currencies, or claim an all-clear it
can't back. if collectors failed it says the number is a floor and lists what
broke; if nothing was configured it says that instead.

## configuration

one yaml file — see `clont.example.yaml` for the full thing. point `$CLONT_CONFIG`
at it, or drop a `clont.yaml` in the working directory; `clont run --config <path>`
beats both.

- the file is **validated on startup** (pydantic), so a typo fails fast with a
  clear message instead of silently doing nothing.
- if there's no config file, clont **writes one with defaults** and carries on.
- cloud access (`role_arn`) lives in the config, not on the command line. see
  [docs/iam.md](docs/iam.md).

the yaml is the only source of truth — no field is overridable by an env var. the
one env var clont reads is `CLONT_CONFIG`, and it just points at the file.

### the settings

**top level**

- `interval_seconds` (int, default `300`) — how often the agent runs a cycle.
- `lookback_days` (int, default `1`) — window for cost / metric queries.
- `log_level` (enum, default `info`) — the daemon's own log volume:
  `debug` / `info` / `warning` / `error` / `critical`. not the same as
  `channels.log.min_severity`, which decides which *events* get logged.
- `aws` (map, default `{}`) — accounts to watch, keyed by alias.
- `channels` (object, default log only) — where events go.

**`aws.<alias>`** — one entry per account. the alias (`prod`, `staging`, whatever)
shows up in notifications and event keys, so two accounts never collide. add
another account by adding another key.

- `role_arn` (str, **required**) — the read-only role clont assumes (via irsa on eks).
- `regions` (list of str, default `[]`) — regions to query.
- `external_id` (str, default `null`) — sts external id, if you use one.
- `cur` (map, default `null`) — your cost and usage report in s3, the free spend
  source: `bucket`, `report_name`, `prefix`, `region` (default `us-east-1`),
  `refresh_minutes` (default `60`) and `include_linked` (default `false` — keep
  only this account's rows out of a payer report; `true` reports every linked
  account under its own alias). legacy cur (gzip csv) only;
  setup is in [docs/iam.md](docs/iam.md). without it, and without
  `finops.allow_cost_explorer`, there's no spend data.
- `members` (map, default `null`) — payer only: discover the org's accounts with
  `organizations:ListAccounts` and assume `role_name` in each, with optional
  `include` / `exclude` account-id lists. members inherit these regions and get no
  cur of their own, so org spend still comes from the payer report once.

if one account's role can't be assumed at startup, clont warns and keeps going
with the rest. it only gives up if *no* account authenticates.

**`finops`**

- `allow_cost_explorer` (bool, default `false`) — read spend from
  `ce:GetCostAndUsage` instead of the cur. billed: $0.01 a request, ~$0.30/mo per
  account at the default daily cadence. off means zero paid cost explorer calls.
- `collect_interval_seconds` (int, default `86400`) — how often spend is really
  fetched, no matter how fast `interval_seconds` ticks. cached records still reach
  the detectors every cycle, so lowering this buys freshness, not coverage.
  `clont run --summary` always forces a full refresh.
- `recommend_interval_seconds` (int, default `3600`) — same, for recommendations.
- `spend_baseline_days` (int, default `28`) — how far back the spike baseline
  looks. ~4 weeks gives you several same-weekday samples; short windows fall back
  to a flat mean.
- `spend_spike_pct` (float, default `50`) — `warn` when a service's latest day
  beats its baseline by more than this percent.
- `spend_min_dollars` (float, default `1`) — ignore services spending less than
  this, so pocket change doesn't page you.
- `budgets` (list, default `[]`) — monthly ceilings. each entry has
  `monthly_limit` (required), `account` (an alias, or `"*"` for all, default
  `"*"`), optional `service` (spelled as the spend source spells it — cur
  `product/ProductName`; leave it out for a whole-account budget) and `currency`
  (default `USD`).
- `budget_warn_pct` (float, default `80`) — `warn` when the forecast reaches this
  much of a budget. `critical` fires once you've actually gone over.
- `forecast_alpha` (float, default `0.5`) — how much the forecast leans on recent
  days (higher = more recent).
- `allow_cloudwatch_metrics` (bool, default `false`) — fall back to the cloudwatch
  idle detectors (ec2 / rds / nat) instead of compute optimizer. billed:
  `GetMetricData` is $0.01 per thousand *metrics*, one per resource per cycle, so
  the bill grows with your fleet. only worth it on an account that isn't enrolled
  in compute optimizer. the next three settings only apply to this fallback.
- `idle_cpu_pct` (float, default `5`) — average cpu % below which ec2/rds counts
  as idle.
- `idle_lookback_days` (int, default `14`) — window the idle averages are taken over.
- `idle_rds_max_connections` (float, default `1`) — average connections below
  which an rds instance counts as idle.
- `snapshot_max_age_days` (int, default `90`) — snapshots older than this are "old".
- `s3_multipart_min_age_days` (int, default `7`) — incomplete multipart uploads
  older than this count as abandoned.
- `s3_cold_min_gb` (float, default `100`) — don't suggest a storage-class
  transition for a bucket with less than this in Standard.
- `ri_sp_min_utilization` (float, default `90`) — flag a savings plan / ri used
  below this percent.
- `ri_sp_min_coverage` (float, default `70`) — flag when eligible usage is covered
  below this percent.
- `nonprod_tags` (map of tag key → values, default `{}`) — the tags that mark
  something as non-prod, e.g. `Environment: [dev, staging, test, qa]`. empty turns
  the off-hours collector off entirely — it never guesses which boxes are non-prod.
- `required_tags` (list of str, default `[]`) — tag keys every cost-bearing
  resource must have. empty turns tag hygiene *and* showback off.
- `showback_unattributed_pct` (float, default `20`) — showback groups spend by
  `required_tags` and warns when this much of it carries no value for a key.
- `transfer_spend_pct` (float, default `15`) — warn when data transfer takes this
  share of an account's spend. 5-15% is normal, past that it's a finding.

**`monitoring`**

- `metrics` (map) — the cloudwatch collection everything here runs on, and the
  only paid call left: `enabled` (bool, default `false`), `services` (list,
  default `[]` = every collector that has metrics), `metrics` (list, default `[]`
  = whatever the collectors ask for), `period_seconds` (int, default `null` = the
  collectors' own granularity), `max_metrics_per_cycle` (int, default `1000`,
  ≈$0.01 a cycle) and `collect_every_seconds` (int, default `null` = every cycle).
  `max_metrics_per_cycle` caps one cycle's spend, `collect_every_seconds` caps the
  day's. with this off the detectors below have nothing to chew on and say so once
  at startup.
- `anomaly_sigma` (float, default `3`) — how many standard deviations from the
  baseline before it's an anomaly.
- `anomaly_min_points` (int, default `6`) — minimum baseline samples before a
  series is allowed to flag anything.
- `free_storage_min_pct` (float, default `10`) — `warn` under this much rds free
  storage.
- `disk_used_max_pct` (float, default `90`) — `warn` over this much redshift disk used.
- `cpu_credit_min_balance` (float, default `20`) — `warn` under this many cpu credits.
- `swap_usage_max_mb` (float, default `50`) — `warn` over this much elasticache swap.
- `disk_full_forecast_days` (float, default `14`) — `warn` when storage is
  projected to hit full within this many days.

**`channels.log`** — always on

- `repeat_hours` (float, default `3.0`) — re-log a standing problem at most this often.
- `min_severity` (enum, default `info`) — drop anything below this.

**`channels.slack` / `channels.discord` / `channels.telegram`** — all optional

- `webhook_url` (str, **required** for slack & discord) — incoming webhook url.
- `bot_token` (str, **required** for telegram) — from @BotFather.
- `chat_id` (str, **required** for telegram) — target chat / channel / group.
- `min_severity` (enum, default `warn`) — drop anything below this.
- `repeat_hours` (float, default `null`) — `null` notifies once per condition; a
  number re-notifies a still-open one that often.

`min_severity` takes `info`, `warn` or `critical`.

## where the dollar figures come from

everything comes out of `clont/finops/aws/prices.json` — public on-demand list
prices, generated offline from the aws price list bulk api. nothing is fetched at
runtime and no iam grant is involved.

they're estimates and clont says so. one rate per instance family at `.large`,
scaled by size; commitment discounts and provisioned iops aren't modelled. a
resource in a region that's missing from the table gets priced at us-east-1 rates
and marked approximate, so the report says "estimated at us-east-1 rates" instead
of passing a guess off as a quote.

public ipv4 has its own key (`public_ipv4_hourly`, the in-use sku). it's the same
$0.005/hr as an idle elastic ip today, but they're separate skus and aws can move
one without the other — if your price table predates the in-use sku, clont falls
back to the idle rate instead of the generic default.

regenerate at release time, prices drift:

```sh
python tools/gen_prices.py          # ~15 min, no credentials needed
```

## status

- development status: active
- license: apache-2.0
