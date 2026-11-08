# clont

a long-running, **read-only** agent that watches your cloud for broken things and
wasted money.

clont sits next to your aws accounts, your own vmware iron and your kubernetes
clusters, looks around every few minutes, and pings you on slack / discord /
telegram when something is wrong or costing you more than it should. every call
it makes is a `describe` / `get` / `list` — it never changes anything.

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

your own iron has no invoice to read, so its spend comes from a rate card you
write — see [own iron](#own-iron-vmware-for-now) below. same records, same
digest, spike, forecast and budgets.

### budgets and forecast

a month-end **forecast** (`info`) from month-to-date plus the days still to come,
each priced at a weighted daily rate times its **weekday** factor — a remainder
full of weekends forecasts lower than one full of mondays. the event carries a
range, and says "too early to be precise" while that range is wide, instead of
quoting the same confident figure on the 2nd as on the 25th.

**budget alerts** against ceilings you set run off the same projection (two
different month-end numbers in one digest would only cost you trust): `warn` when
the forecast gets close or is heading over, `critical` once you've actually blown
through it. plain arithmetic — no model, no extra api calls.

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
- **commitments about to lapse** — reserved instances and savings plans in their
  last 60 / 30 / 7 days. when one ends, the usage under it goes back to on-demand
  overnight and the bill only tells you days later; the end date rides along on
  the same free describes. the tiers are separate findings on purpose, so an alert
  at 60 days doesn't silence the one at 7. sagemaker and database plans are
  watched too — they cover no ec2, but they still expire.
- **graviton candidates** — x86 instances compute optimizer would move to arm64,
  with the migration effort it reports. these need an explicit arm64 preference on
  the call, so without it they look absent rather than unasked-for. it's a
  separate finding from rightsizing: same instance can have both.
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

- **own iron and kubernetes** — another 8 kinds of waste on your own hardware and
  7 that only a cluster can see. both have their own sections below.

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

## own iron (vmware, for now)

a datacenter sends you no invoice, so there is nothing to read. clont doesn't
guess a price either: **you write down what a pool costs per month, clont
measures that pool's capacity and divides.** out comes a $/vcpu-hour,
$/gb-month and $/gib-month in your own money, and from there the same cost
records aws produces.

**a pool is a cluster**, not a site. a gen9 box and a gen11 box don't cost the
same per core, and a site average hides exactly the comparison you built the
report for. so the card has two levels: the site card holds what's true for the
whole floor, a cluster card overrides it **line by line**, and a cluster you
never mention is priced on the site card alone.

the lines are `hardware_capex` + `lifetime_months` (or a monthly
`hardware_amortization`), `power_and_cooling`, `rack_and_network`, `licenses`,
`support` and `staff` — staff is off by default because it's arguable.
`weights` split the pool over cpu / ram / storage, must add up to 1.0, and get
printed next to every number they produced.

**how it reads the site.** one pass over vcenter (or a standalone esxi host)
under the built-in **Read-Only** role — clusters, hosts, vms and datastores, a
few property reads, no agent inside a guest. with `usage_window_days` above zero
it also reads perf counters and keeps a **p95 per vm**, which is the only reason
idle and rightsize advice is possible. a vm it couldn't measure is simply not
advised about: an absent measurement is not zero usage.

**what the money looks like.** a daily cost record per vm — so showback, the
digest, spikes, forecast and budgets all work with no changes — plus a
`headroom` line for the slice of the cluster nobody is using. you pay for the
whole cluster whether it's full or not, and without that line the site total
would come out under the real bill. a vm is billed what it **reserved** and for
the disk it **occupies**, never the thin promise.

| finding | what it is |
|---|---|
| `stopped-vm` | powered off, still holding its disk blocks |
| `template-disk` | same, for a golden image nobody is going to boot |
| `idle-vm` | powered on, ran at nothing all window — on cpu **and** ram |
| `rightsize-vm` | busy enough to keep, more vcpu / ram than its p95 ever needed |
| `zombie-host` | powered on, in the cluster, carrying no load |
| `powered-off-host` | still in the capex and still licensed, so still billed |
| `unaccounted-storage` | datastore space no vm claims — isos, orphaned vmdks, dead folders |
| `unmounted-datastore` | an array no host mounts, so none of it can be used |
| `orphan-vm` | vcenter lists it on no host, usually a failed migration |
| `thin-overcommit` | a risk, not a saving: reports $0 and what the promises add up to |

two things that keep the numbers honest. **per-cluster findings are priced on
your own card, so they're exact** (`approximate=False`, which no aws finding
gets) — site-wide leftovers have no cluster, so they get the site's mean
$/gib-month and say they're approximate. and **what a vm hands back is divided
by the pool's oversubscription**: at an ordinary 4:1 ratio, switching off a
4-vcpu vm frees one physical core, not four. without that, 1250 test vms advised
$142k/month of savings on a cluster that costs $108k.

## kubernetes

a cluster is **not a provider here, it's a source**. `priced_by` names a site
under `onprem:` or an account under `aws:`, and it's required: clont's k8s number
is the continuation of a pool it already priced.

```
pool card → the node vm's cost record → the node → the namespace's requests
```

so nothing is invented. a node clont can't place on priced iron is reported
**unmapped**, never priced at zero, and the startup log says how many nodes of
each cluster were mapped and why the rest weren't.

**namespace showback** splits a node's cost four ways, and the four add back up:
each namespace gets its requests' share of the node's **capacity**, `(kubelet)`
is the reservation nobody can schedule into, `(unrequested)` is schedulable iron
no pod asked for, `(node-storage)` is the vm's disk. the cpu/ram split comes off
your own card, not a 50/50 guess. `warn` when more than
`k8s_unrequested_pct` of a cluster's priced iron no pod requested.

access is whatever the kubeconfig or the pod's service account already has:
`get`/`list` on nodes, pods, namespaces, pvcs, pvs and `pods.metrics.k8s.io`.
only nodes and pods are needed to price the cluster.

| finding | what it is |
|---|---|
| `oversized-node-pool` | the pods still fit after dropping whole node vms — worth those vms |
| `double-overcommit` | nothing can be dropped, but the hypervisor oversubscribes *and* the nodes are half empty. $0, a risk line |
| `rightsize-workload` | requests above the busiest replica's p95, per node pool |
| `underrequested-workload` | ram working set over the request — $0, the pod first in line to be killed |
| `unmounted-pvc` | a bound claim no pod references |
| `abandoned-namespace` | no pod at all, live or pending, still holding claims |
| `released-pv` | the claim is gone and `reclaimPolicy: Retain` kept the volume |

two notes worth having. a `rightsize-workload` offers back **capacity, not
cash** — shrinking a request moves money into `(unrequested)`, the invoice only
moves when the cluster can drop a node, so the finding says it's approximate.
and pvcs are what keeps the hypervisor side honest: from vcenter those blocks
look like `unaccounted-storage`, so whatever a cluster owns up to is netted off
the site's gap instead of being counted twice.

## platform

- **many accounts** — as many as you like, keyed by an alias you choose. one
  account failing gets skipped, not fatal. on an org, point clont at the payer
  and set `members.role_name`: it lists the linked accounts itself and splits the
  payer's spend per account, so each one gets its own digest, spike and budget.
- **sites and clusters are aliases too** — `onprem.<site>` and
  `kubernetes.<cluster>` are keyed the same way as accounts, and the alias flows
  into every event key, so nothing collides.
- **read-only by construction** — every call is a `Describe*` / `Get*` (on
  vmware the built-in Read-Only role, on k8s `get`/`list`). see
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
  `include_tax` (default `true`). legacy cur (gzip csv) only;
  setup is in [docs/iam.md](docs/iam.md). without it, and without
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

## running it on kubernetes

```sh
helm install clont ./charts/clont -n clont --create-namespace -f my-values.yaml
helm test clont -n clont       # validates the mounted config, makes no api call
```

a single-replica deployment, the config as a configmap (or a secret when it holds
webhooks or an api key), and a read-only clusterrole when clont also prices the
cluster it runs in. details in [charts/clont/README.md](charts/clont/README.md).

## where the dollar figures come from

everything comes out of `clont/finops/aws/prices.json` — public on-demand list
prices, generated offline from the aws price list bulk api. nothing is fetched at
runtime and no iam grant is involved.

instance rates are per *type*, per region, as aws publishes them — ~1400 types
including `.metal`. ebs carries the provisioned iops and gp3 throughput skus too,
so a gp2 -> gp3 saving is net of the performance the gp3 has to buy back.

they're still estimates and clont says so. commitment discounts are flat guesses,
io2's cheaper iops tiers above 32k aren't modelled, and a type launched after the
table was generated falls back to its family's `.large` scaled by size. a resource
in a region that's missing from the table gets priced at us-east-1 rates and
marked approximate, so the report says "estimated at us-east-1 rates" instead of
passing a guess off as a quote.

public ipv4 has its own key (`public_ipv4_hourly`, the in-use sku). it's the same
$0.005/hr as an idle elastic ip today, but they're separate skus and aws can move
one without the other — if your price table predates the in-use sku, clont falls
back to the idle rate instead of the generic default.

**own iron and kubernetes never touch that table.** their numbers come from the
rate card you wrote, divided by the capacity clont measured — your money, not a
list price. that's why a per-cluster on-prem finding is exact where an aws one is
an estimate.

regenerate at release time, prices drift:

```sh
python tools/gen_prices.py          # ~15 min, no credentials needed
```

## status

- development status: active
- license: apache-2.0
