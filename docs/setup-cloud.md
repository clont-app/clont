# quick start: aws

follow this top to bottom and you end up with an agent that watches your aws
accounts and messages you when something breaks or costs too much. about 30
minutes, most of it waiting for aws.

**nothing here changes your infrastructure.** every call clont makes is a
`describe` / `get` / `list`.

## before you start

- an aws account, and permission to create an iam role in it (or someone who can
  do steps 1 and 2 for you)
- somewhere to run a small container later: a linux box with docker, or a
  kubernetes cluster. step 3 only needs python 3.11+ on your laptop.
- a slack / discord / telegram webhook if you want pings, step 6

## words used below

| word | means |
|---|---|
| cur | cost and usage report — a csv of your daily spend that aws writes to s3 for you, for free. clont reads your spend from there. |
| role | the identity clont assumes in each account to look around. read-only. |
| trust policy | the part of the role that says *who* may assume it. forget it and clont sees 0 accounts. |
| irsa | on eks, a way to give a pod an iam role so you don't handle access keys. |
| alias | the short name you give an account in the config (`prod`, `dev`). it shows up in every message. |

## the order, in short

do the aws part first, install second. clont can't see anything until the role
exists, and spend stays empty until aws writes the first report — which takes up
to a day, so start that clock early.

1. a bucket and a cost export in it — only if you want spend (step 1)
2. a read-only role in each account (step 2)
3. then `pip install`, docker or helm (step 3 and step 7)

## 1. make the bucket and the cost export

skip this if you only want health and idle checks — clont runs fine without it,
just with no spend, budgets or forecast.

in the payer (or single) account: billing → data exports → create → standard
data export (cur 2.0).

1. **export name** — e.g. `clont-cur`.
2. tick **include resource ids**, pick **csv + gzip** (not parquet), "overwrite
   existing report", daily.
3. **s3 bucket** — choose "create a bucket", e.g. `my-billing-bucket`. aws adds
   the bucket policy that lets it write there by itself. an existing bucket works
   too.
4. **s3 path prefix** — e.g. `reports`.
5. create.

write down three things, the role needs them in step 2: bucket, prefix, export
name. the files land under `s3://<bucket>/<prefix>/<export-name>/`.

two things to know:

- aws takes **up to 24 hours** to write the first report. health checks work
  immediately; spend, spikes, forecast and budgets stay empty until the file
  exists.
- for per-team showback you also have to activate your tag keys as **cost
  allocation tags** in the billing console. until you do, the cur has no column
  for them and every dollar reads as unattributed.

## 2. make a read-only role in each account

one role per aws account you want watched. in the console, iam → roles → create
role:

1. trusted entity — the identity clont will run as. on eks that's the irsa role
   of its service account; on a vm, the user whose access keys it uses. the exact
   json is in [iam.md](iam.md#trust-policy).
2. permissions — paste the policy from
   [iam.md](iam.md#the-permissions-policy). it is read-only. the one line to edit
   is the `Resource` in `ClontCUR`, built from step 1:
   `arn:aws:s3:::<bucket>/<prefix>/<export-name>/*`. no export? leave the
   example line, it does no harm.
3. name it `clont-readonly` and copy the role arn
   (`arn:aws:iam::111111111111:role/clont-readonly`) — you need it in step 4.

have an aws organization? make the role once per member account with the **same
name**, point clont at the payer and let it find the rest:
[iam.md](iam.md#several-accounts-the-payer-fan-out).

clont finds the export by itself through `bcm-data-exports`, so nothing from
step 1 goes into the yaml.

## 3. install it on your laptop first

you will run it on a server later. installing locally first means you can check
your aws access before any deploy.

```sh
python3 -m venv ~/clont-venv
~/clont-venv/bin/pip install clont
~/clont-venv/bin/clont --version
```

the rest of this page writes `clont` — use `~/clont-venv/bin/clont`, or activate
the venv (`source ~/clont-venv/bin/activate`).

## 4. write clont.yaml

one file, and it is the whole configuration — there are no env vars that
override it. save this as `clont.yaml` and replace the marked values:

```yaml
interval_seconds: 300          # how often it looks around
aws:
  prod:                        # <- your alias, anything short
    role_arn: arn:aws:iam::111111111111:role/clont-readonly   # <- step 2
    regions: [us-east-1, eu-west-1]                           # <- yours
    # cur: found by itself (step 1); a cur: block here overrides it
channels:
  log:
    min_severity: info         # print everything to stdout for now
```

a second account is another key under `aws:` with its own alias and role arn.
every key there is is listed in
[../clont.example.yaml](../clont.example.yaml).

## 5. check it works, before deploying anything

```sh
export AWS_PROFILE=...     # whatever gets you into aws from your laptop
CLONT_CONFIG=./clont.yaml clont run --once --summary -
```

one cycle, a summary to the screen, then it exits. you should see something like:

```
clont scan summary (2026-11-09T10:21:04+00:00, clont 0.3.4)
accounts:   prod
duration:   38.2s

collected:
  metrics=0 costs=412 recommendations=7 health=96

events:     5
  by severity: info=4, warn=1
  ...
estimated monthly savings:
  128.40 USD
```

read it in this order:

1. **`accounts:`** — your alias is there. `none` means the config never loaded.
2. **is there an `errors:` block at the bottom?** that is the one that matters.
   it names each collector that failed and why — a missing grant shows up here,
   not as an empty report.
3. **`costs=`** — a number above 0 means the cur was found and read. `costs=0` on
   a fresh export is normal for the first day (see step 1).
4. `health=` and `recommendations=` — what it checked and what it wants to tell
   you.

no errors and your alias listed? access is good. if not, jump to [when it looks
wrong](#when-it-looks-wrong).

`--summary report.txt` instead of `-` writes the shareable report (dollar figure
first), `--summary out.json` the machine-readable one.

## 6. add notifications

the `log` channel just prints. add the channels you want — each has its own
severity floor, so slack can stay quiet while the log keeps everything:

```yaml
channels:
  slack:
    webhook_url: https://hooks.slack.com/services/XXX/YYY/ZZZ
    min_severity: warn
    repeat_hours: 24        # re-notify a still-broken thing once a day
  telegram:                 # optional, same idea
    bot_token: "123456:ABC-DEF"
    chat_id: "-1001234567890"
    min_severity: critical
```

a slack webhook comes from a slack app → incoming webhooks → add to a channel.
for telegram, talk to `@BotFather` for the token and add the bot to the chat.

re-run step 5 after editing — a message should land in the channel.

**a config with a webhook in it is a secret.** on kubernetes put the whole yaml
in a Secret (step 7), not in `values.yaml`, and keep the local copy `chmod 600`.

## 7. run it for real

pick one.

**docker, on any linux box** — simplest:

```sh
docker run -d --name clont --restart unless-stopped \
  -v $PWD/clont.yaml:/etc/clont/clont.yaml:ro \
  -e AWS_ACCESS_KEY_ID=... -e AWS_SECRET_ACCESS_KEY=... \
  ghcr.io/clont-app/clont:0.3.4
docker logs -f clont
```

**kubernetes, with helm** — the chart is not published to a registry yet, so
install it from a checkout:

```sh
git clone https://github.com/clont-app/clont && cd clont
cp charts/clont/examples/values-cloud.yaml my-values.yaml
# edit my-values.yaml: aliases, role arns, cur, regions
helm install clont ./charts/clont -n clont --create-namespace -f my-values.yaml
helm test clont -n clont        # checks the config the pod mounted, no api calls
kubectl -n clont logs deploy/clont -f
```

on eks, give the pod an identity instead of keys:

```yaml
serviceAccount:
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::111111111111:role/clont
rbac: {create: false}     # aws-only, nothing reads the cluster
```

off eks, `envFrom` a secret holding `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY`. every chart value is listed in
[../charts/clont/README.md](../charts/clont/README.md).

that's it — it now runs a cycle every `interval_seconds` and notifies you.

## 8. what to tune once it runs

no rush, the defaults work. in rough order of value:

- **`finops.budgets`** — monthly ceilings per account or per service. no budget,
  no budget alerts (you still get the month-end forecast).
- **`finops.spend_spike_pct`** (50) and **`spend_min_dollars`** (1) — how loud
  spike alerts are. the baseline is the same weekday a week ago, so mondays
  aren't spikes.
- **`finops.required_tags` / `nonprod_tags`** — empty by default, which switches
  off tag hygiene and showback. fill them once your tags are real.
- **`monitoring.metrics.enabled`** and **`finops.allow_cloudwatch_metrics`** —
  **the two settings that cost money** (cloudwatch api calls). off by default,
  which is why you see no cpu anomalies or disk-full warnings. idle detection
  already works for free through compute optimizer.

## when it looks wrong

| what you see | usually |
|---|---|
| `accounts: none` | the config wasn't loaded — check `CLONT_CONFIG` points at the file |
| 0 accounts scanned, or an assume-role error | the trust policy: the identity clont runs as isn't allowed to assume the role |
| `costs=0`, health fine | no export found (see the startup log for a missing grant), the first export hasn't landed yet (24h), or a `cur:` override doesn't match s3 |
| spend a day or two behind | normal — aws rewrites the report a few times a day |
| every cost line unattributed | the tag keys were never activated as cost allocation tags |
| no anomaly or disk-full events | `monitoring.metrics.enabled` is false, the default |
| pod restarting right after start | a config error, fatal on purpose — the log names the key |
