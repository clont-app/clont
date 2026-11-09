# quick start: aws

zero to a running agent on one or more aws accounts. everything here is
read-only; the grants are in [iam.md](iam.md) and the keys in
[../clont.example.yaml](../clont.example.yaml).

## 1. a read-only role per account

one role per account, assumed by clont. attach the policy from
[iam.md](iam.md#the-permissions-policy) and a trust policy that lets clont's own
identity assume it ([iam.md](iam.md#trust-policy)):

- on eks — the irsa role on clont's service account
- anywhere else — the user/role whose keys the agent runs with

on an organization you only make one role *shape* and let clont find the
members: point it at the payer and set `members.role_name`
([iam.md](iam.md#several-accounts-the-payer-fan-out)).

## 2. the cost and usage report

spend comes from the cur your account already writes to s3 — free, where cost
explorer bills $0.01 a request. create it once (legacy csv/gzip, not parquet) and
note four things: bucket, report name, prefix, bucket region.
[iam.md](iam.md#spend-the-cost-and-usage-report) has the exact settings and the
s3 read grant.

without a cur, health checks still work and **spend, spikes, forecast, budgets
and showback have no data**. `finops.allow_cost_explorer: true` is the paid
fallback.

if you want tag showback and tag hygiene, activate those tag keys as **cost
allocation tags** in billing. otherwise the cur has no column for them and every
line reads as unattributed.

## 3. write the config

`clont.yaml` is the whole configuration — no env var overrides anything. minimal
working file:

```yaml
interval_seconds: 300
aws:
  prod:
    role_arn: arn:aws:iam::111111111111:role/clont-readonly
    regions: [us-east-1, eu-west-1]
    cur:
      bucket: my-billing-bucket
      report_name: clont-cur
      prefix: reports
      region: us-east-1
channels:
  log:
    min_severity: info
```

add an account by adding another key under `aws:`. the alias (`prod`) is what
shows up in every event, so two accounts never collide.

## 4. prove the access before deploying anything

```sh
pip install clont
CLONT_CONFIG=./clont.yaml clont run --once --summary -
```

the summary is the point: accounts scanned, how much was collected, events by
severity, estimated savings, top services by spend, and **every collector that
failed**. so "nothing found" is distinguishable from "the role couldn't read".
`--summary` forces a full refresh, caches ignored.

a `.txt` path gives the shareable report instead, `.json` one for scripts.

## 5. run it for real

helm, from [values-cloud.yaml](../charts/clont/examples/values-cloud.yaml):

```sh
helm install clont oci://ghcr.io/clont-app/charts/clont -n clont \
  --create-namespace -f values-cloud.yaml
helm test clont -n clont        # validates the mounted config, no api calls
```

irsa goes in `serviceAccount.annotations`; off eks, `envFrom` a secret with
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`. `rbac.create: false` — nothing
reads the cluster. details in [../charts/clont/README.md](../charts/clont/README.md).

docker, anywhere else:

```sh
docker run -d --name clont -v $PWD/clont.yaml:/etc/clont/clont.yaml:ro \
  ghcr.io/clont-app/clont:0.3.1
```

## 6. notifications

the log channel is always on. add the ones you want, each with its own floor and
repeat throttle:

```yaml
channels:
  slack:
    webhook_url: https://hooks.slack.com/services/XXX/YYY/ZZZ
    min_severity: warn
    repeat_hours: 24        # omit = notify once per condition
  telegram:
    bot_token: "123456:ABC-DEF"
    chat_id: "-1001234567890"
    min_severity: critical
```

**a config with a webhook in it is a secret.** on k8s put it in a Secret and set
`existingSecret`, not in `values.yaml`.

## 7. what to tune first

- `finops.budgets` — monthly ceilings per account, or per service by its cur
  name. `account: "*"` covers every account. without a budget there are no
  budget alerts, only the forecast.
- `finops.spend_spike_pct` (50) and `spend_min_dollars` (1) — how loud spike
  detection is. the baseline is the same-weekday median, so mondays aren't
  spikes.
- `finops.required_tags` / `nonprod_tags` — empty by default, which disables tag
  hygiene, showback and the off-hours advice. fill them when the tags exist.
- `monitoring.metrics.enabled` and `finops.allow_cloudwatch_metrics` — **the two
  that cost money.** off by default, which leaves anomaly detection, thresholds
  and the disk-full forecast inert. idle detection comes from compute optimizer
  for free; turn cloudwatch on only for an account that isn't enrolled.

## when it looks wrong

| what you see | usually |
|---|---|
| 0 accounts scanned | the trust policy — clont's identity can't assume the role |
| spend empty, health fine | no `cur:` block, or the prefix/report name doesn't match the s3 layout |
| spend a day or two behind | normal, aws rewrites the report a few times a day (`refresh_minutes`) |
| every cost line unattributed | the tag keys were never activated as cost allocation tags |
| no anomaly or disk-full events | `monitoring.metrics.enabled` is false, which is the default |
| pod restarting at startup | a config error — it's fatal on purpose, the log says which key |
