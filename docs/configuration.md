# configuration

setting it up from scratch, step by step:

- **[setup-cloud.md](setup-cloud.md)** — aws: the role, the cost and
  usage report, the first scan, helm or docker.
- **[setup-onprem.md](setup-onprem.md)** — own iron: the vcenter
  read-only account, the rate card, kubernetes as a source. also covers running
  both estates in one release.

the reference below is the same keys, one by one.

one yaml file — see `clont.example.yaml` for the full thing. point `$CLONT_CONFIG`
at it, or drop a `clont.yaml` in the working directory; `clont run --config <path>`
beats both.

- the file is **validated on startup** (pydantic), so a typo fails fast with a
  clear message instead of silently doing nothing.
- if there's no config file, clont **writes one with defaults** and carries on.
- cloud access (`role_arn`) lives in the config, not on the command line. see
  [docs/iam.md](iam.md).

the yaml is the only source of truth — no field is overridable by an env var. the
one env var clont reads is `CLONT_CONFIG`, and it just points at the file.

## the settings

**top level**

- `interval_seconds` (int, default `300`) — how often the agent runs a cycle.
- `lookback_days` (int, default `1`) — window for cost / metric queries.
- `log_level` (enum, default `info`) — the daemon's own log volume:
  `debug` / `info` / `warning` / `error` / `critical`. not the same as
  `channels.log.min_severity`, which decides which *events* get logged.
- `aws` (map, default `{}`) — accounts to watch, keyed by alias.
- `onprem` (map, default `{}`) — your own sites, keyed by alias.
- `kubernetes` (map, default `{}`) — clusters to read, keyed by alias.
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
  account under its own alias). three more decide what a day's spend *means*:
  `amortize` (default `true` — spread ri / savings-plan fees over their term, so
  an all-upfront commitment isn't a one-day spike), `include_credits` (default
  `true`; `false` gives gross spend, before credits and refunds) and
  `include_tax` (default `true`). cur 2.0 or legacy cur, gzip csv only;
  setup is in [docs/iam.md](iam.md). without it, and without
  `finops.allow_cost_explorer`, there's no spend data.
- `members` (map, default `null`) — payer only: discover the org's accounts with
  `organizations:ListAccounts` and assume `role_name` in each, with optional
  `include` / `exclude` account-id lists. members inherit these regions and get no
  cur of their own, so org spend still comes from the payer report once.

if one account's role can't be assumed at startup, clont warns and keeps going
with the rest. it only gives up if *no* account authenticates.

**`onprem.<site>`** — one entry per site of your own iron.

- `rate_card` (map) — what the site costs a month:
  `hardware_capex` + `lifetime_months` (or `hardware_amortization` directly),
  `power_and_cooling`, `rack_and_network`, `licenses`, `support`, `staff`. every
  line is optional, only the merge of site + cluster has to price something — and
  it's checked at startup, not at the first collection.
- `weights` (map, default `{cpu: 0.5, ram: 0.3, storage: 0.2}`) — how the pool
  cost splits. must sum to 1.0.
- `clusters` (map, default `{}`) — per-cluster `rate_card` / `weights` that
  override the site's, line by line. optional: clont finds the clusters itself,
  this only prices the ones that differ. two datacenters behind one vcenter can
  each hold a `prod` cluster, so `DC0/prod` is accepted as a key.
- `inventory` (map, default `null`) — where to read the site:
  `kind` (`vsphere`), `endpoint` (host, no scheme), `username`, exactly one of
  `password` / `password_env`, `port` (default `443`), `verify_ssl` (default
  `true`), `ca_bundle` (a private ca, instead of turning tls off) and
  `usage_window_days` (default `14`, max `30` — `0` reads no perf counters, and
  then every vm is billed what it reserved with no idle / rightsize advice).
  leaving `inventory` out is fine: the site is priced and idle, which is the
  normal state while the read-only account is still being granted.

**`kubernetes.<cluster>`** — one entry per cluster.

- `priced_by` (str, **required**) — the alias under `onprem:` or `aws:` whose
  pool pays for these nodes. it has to be configured, or startup fails.
- `kubeconfig` (str, default `null` = the usual search) and `context` (default
  the kubeconfig's current one), or `in_cluster` (bool, default `false`) for the
  pod's own service account. not both.
- `match_by_name` (bool, default `true`) — last-resort match, node name == vm
  name. `false` means an unidentifiable node is reported, not guessed.
- `usage` (enum, default `metrics-server`) — `metrics-server`, `prometheus` or
  `off`. it only buys sizing advice; the cluster is fully priced either way.
- `usage_min_samples` (int, default `24`) — metrics-server keeps no history, so
  this many passes have to pile up before a workload is advised about.
- `prometheus_url` (**required** for `usage: prometheus`),
  `prometheus_window_days` (default `14`) and `prometheus_step_minutes`
  (default `5` — widen it on a big cluster rather than shortening the window).
- `timeout_seconds` (int, default `30`).

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

own iron:

- `onprem_min_savings_usd` (float, default `1`) — per finding per month, below it
  is noise.
- `onprem_unaccounted_min_gib` (float, default `100`) and
  `onprem_unaccounted_min_pct` (float, default `10`) — datastore space no vm
  claims. both have to pass, so a big busy site doesn't flag on rounding.
- `onprem_thin_overcommit_ratio` (float, default `1.5`) — thin promises over
  capacity before it's a risk line.
- `onprem_idle_ram_pct` (float, default `20`) — p95 ram below which a running vm
  is idle. cpu reuses `idle_cpu_pct`, and a vm has to be quiet on both.
- `onprem_rightsize_target_pct` (float, default `70`) — headroom left over the
  p95 when shrinking. a vm already near that target yields nothing and emits no
  finding.

kubernetes:

- `k8s_unrequested_pct` (float, default `50`) — `warn` when this share of a
  cluster's priced iron no pod requested. higher than the tag threshold on
  purpose: a cluster with no headroom is one node failure from pending pods.
- `k8s_pool_unrequested_pct` (float, default `25`) — the floor a node-pool
  finding needs before it fires. a different number from the one above.
- `k8s_overcommit_ratio` (float, default `1.25`) — hypervisor vcpu overcommit
  that turns a half-empty pool into a `double-overcommit`.
- `k8s_claim_min_gib` (float, default `10`) — a pvc smaller than this is noise.
- `k8s_claim_min_age_days` (float, default `7`) — a younger unmounted pvc is a
  deploy in progress.

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
