# what clont finds

## health

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

## spend

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

## budgets and forecast

a month-end **forecast** (`info`) from month-to-date plus the days still to come,
each priced at a weighted daily rate times its **weekday** factor — a remainder
full of weekends forecasts lower than one full of mondays. the event carries a
range, and says "too early to be precise" while that range is wide, instead of
quoting the same confident figure on the 2nd as on the 25th.

**budget alerts** against ceilings you set run off the same projection (two
different month-end numbers in one digest would only cost you trust): `warn` when
the forecast gets close or is heading over, `critical` once you've actually blown
through it. plain arithmetic — no model, no extra api calls.

## savings findings

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
