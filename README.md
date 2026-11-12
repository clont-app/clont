# clont

[![ci](https://github.com/clont-app/clont/actions/workflows/ci.yml/badge.svg)](https://github.com/clont-app/clont/actions/workflows/ci.yml)
[![pypi](https://img.shields.io/pypi/v/clont)](https://pypi.org/project/clont/)

a long-running, **read-only** agent that watches your cloud for broken things and
wasted money.

clont sits next to your aws accounts, your own vmware iron and your kubernetes
clusters, looks around every few minutes, and pings you on slack / discord /
telegram when something is wrong or costing you more than it should. every call
it makes is a `describe` / `get` / `list` — it never changes anything.

one scan of a sample account (`clont run --summary report.txt`):

```
====================================================================
 CLOUD WASTE REPORT
====================================================================

  you're wasting
    $494.34 / month
    $5,932.08 / year

  15 finding(s) across 2 account(s): prod, staging
  scanned 2026-11-11 09:30 UTC  |  clont 0.3.3  |  6.412s

--------------------------------------------------------------------
 WHERE THE MONEY GOES
--------------------------------------------------------------------

  $246.40/mo   x4    unattached-ebs (ebs)
                 - vol-0e4f1a2b3c4d5e6f7 [eu-west-1]  $176.00/mo  Unattached gp3 (2000 GiB) — delete
                 - vol-03a9b8c7d6e5f4a3b [eu-west-1]  $44.00/mo  Unattached gp3 (500 GiB) — delete
                 - vol-0b1c2d3e4f5a6b7c8 [eu-west-1]  $17.60/mo  Unattached gp3 (200 GiB) — delete
                 ... and 1 more

  $105.12/mo   x3    idle-nat (ec2)
                 - nat-0a1b2c3d4e5f60718 [eu-west-1]  $35.04/mo  NAT gateway idle 14d (≈0 bytes processed) — delete if unused
                 - nat-07c9e2f1a3b4d5e6f [eu-west-1]  $35.04/mo  NAT gateway idle 14d (≈0 bytes processed) — delete if unused
                 - nat-0f3e8d2c1b0a99887 [us-east-1]  $35.04/mo  NAT gateway idle 14d (≈0 bytes processed) — delete if unused

  ... 4 more kinds, then top spend and health
```

the numbers are made up, the format is the real one.

for the bigger picture (free local tier vs paid hosted one, how the pieces fit)
see [docs/architecture.md](docs/architecture.md).

## install

three ways, simplest first. all of them read one file, `clont.yaml` — the
minimal one is in [docs/setup-cloud.md](docs/setup-cloud.md#4-write-clontyaml)
(aws) or [docs/setup-onprem.md](docs/setup-onprem.md) (own iron). the iam role it
needs is in [docs/iam.md](docs/iam.md).

### pip

python 3.11+. the quickest way to see what clont finds:

```sh
python3 -m venv ~/clont-venv
~/clont-venv/bin/pip install clont                  # aws only
~/clont-venv/bin/pip install 'clont[vsphere,k8s]'   # + own iron and kubernetes
```

one scan, a summary to the screen, then it exits:

```sh
CLONT_CONFIG=./clont.yaml ~/clont-venv/bin/clont run --once --summary -
```

drop `--once --summary -` and it keeps running, a cycle every
`interval_seconds`.

### docker

for any linux box. the image reads `/etc/clont/clont.yaml`:

```sh
docker run -d --name clont --restart unless-stopped \
  -v $PWD/clont.yaml:/etc/clont/clont.yaml:ro \
  -e AWS_ACCESS_KEY_ID=... -e AWS_SECRET_ACCESS_KEY=... \
  ghcr.io/clont-app/clont:0.3.3
docker logs -f clont
```

### kubernetes (helm)

the chart isn't in a registry yet, install it from a checkout:

```sh
git clone https://github.com/clont-app/clont && cd clont
cp charts/clont/examples/values-cloud.yaml my-values.yaml   # or values-onprem / values-hybrid
helm install clont ./charts/clont -n clont --create-namespace -f my-values.yaml
helm test clont -n clont       # checks the mounted config, no api calls
```

on eks give the pod an irsa role instead of keys. all chart values are in
[charts/clont/README.md](charts/clont/README.md), setup guides in
[docs/setup-cloud.md](docs/setup-cloud.md) and [docs/setup-onprem.md](docs/setup-onprem.md).

## what it finds

- **health** — ec2, rds, eks, ecs, load balancers, expiring certs and more, plus
  anomaly alerts against a seasonal baseline instead of fixed thresholds.
- **spend** — a daily digest and spike alerts, read from the cost and usage
  report your account already writes. free, no cost explorer.
- **budgets and forecast** — a month-end projection that warns before you go
  over, not after.
- **savings** — rightsizing, idle resources, unattached ebs, gp2 → gp3,
  commitments to buy or about to lapse, graviton, s3 waste, data transfer, tag
  hygiene and showback. each with a monthly dollar figure.
- **public ipv4** — every address counted ($3.65/month each since 2024), the
  unattached and secondary ones flagged.
- **own iron** — vmware priced off a rate card you write: idle and stopped vms,
  zombie hosts, storage nobody claims.
- **kubernetes** — namespace showback on priced nodes, oversized node pools,
  unmounted pvcs.

how each one works: [docs/findings.md](docs/findings.md).

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
evidence under it — the sample at the top of this file is exactly this output.

findings are grouped by kind and sorted by money, biggest first. then top spend,
health, and any collector errors.

two things it won't do: add up different currencies, or claim an all-clear it
can't back. if collectors failed it says the number is a floor and lists what
broke; if nothing was configured it says that instead.

## docs

- [setup-cloud.md](docs/setup-cloud.md) — aws, step by step
- [setup-onprem.md](docs/setup-onprem.md) — own iron and kubernetes, step by step
- [iam.md](docs/iam.md) — the read-only role
- [configuration.md](docs/configuration.md) — every `clont.yaml` key
- [findings.md](docs/findings.md) — what it watches and how
- [events.md](docs/events.md) — severities, keys, when a channel fires
- [pricing.md](docs/pricing.md) — where the dollar figures come from
- [architecture.md](docs/architecture.md) — free local tier vs hosted one
- [api.md](docs/api.md) — the optional api uplink

## status

- development status: active
- license: apache-2.0
