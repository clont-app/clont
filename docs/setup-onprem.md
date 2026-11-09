# quick start: your own iron

follow this top to bottom and you end up with an agent that watches your own
vmware (and the kubernetes clusters on it), tells you what it costs and where the
waste is. no aws anywhere. about 30 minutes.

**nothing here changes your infrastructure.** the vcenter account is read-only and
clont only ever reads.

the one difference from cloud: **there is no invoice to read.** you write down
what a pool of iron costs per month, clont measures the capacity in vcenter and
divides. that table is called the *rate card* and it is the only thing nobody can
work out for you. everything after it — daily digest, spikes, month-end forecast,
budgets, waste findings — works exactly like the aws side.

## before you start

- a vcenter (or a standalone esxi host), and someone who can create a user in it
- somewhere to run a small container: a linux box with docker, or a kubernetes
  cluster. step 1 only needs python 3.11+ on your laptop.
- rough monthly numbers for your hardware: power, rack/network, licenses, and
  what the servers cost to buy. estimates are fine — step 3 explains why.
- the pod/container must reach vcenter on port 443

## words used below

| word | means |
|---|---|
| site | one alias in the config, usually a datacenter or a floor (`dc1`). |
| rate card | the monthly cost of a pool of iron. you write it, clont divides it by capacity. |
| cluster card | the same lines, for one vsphere cluster, overriding the site's. |
| weights | how the pool's cost splits over cpu / ram / storage. must add up to 1.0. |
| priced_by | on a kubernetes cluster: which site's iron its nodes are, so pods can be priced. |

## 1. install it on your laptop first

you will run it on a server later. installing locally first means you can check
the vcenter access before any deploy.

```sh
python3 -m venv ~/clont-venv
~/clont-venv/bin/pip install 'clont[vsphere,k8s]'
~/clont-venv/bin/clont --version
```

note the `[vsphere,k8s]` — a plain `pip install clont` is aws-only and will fail
with `ModuleNotFoundError: pyvmomi`. the docker image already has both.

below, `clont` means `~/clont-venv/bin/clont`.

## 2. a read-only vcenter account

no custom role needed:

1. create a user, e.g. `clont-ro@vsphere.local` (an ad user works too)
2. in vcenter → permissions, assign it the built-in **Read-Only** role
3. assign it **at the vcenter root**, with "propagate to children" ticked

that's the whole requirement. what it reads, and why root rather than per-cluster:
[iam.md](iam.md#own-iron-a-vsphere-read-only-account).

the password never goes on a command line. you either put it in the yaml (file
mode 600) or name an env var with `password_env` — exactly one of the two.

## 3. write the rate card

**a pool is a cluster, not a site.** a 2016 box and a 2024 box don't cost the
same per core, and a site-wide average hides the comparison you were after. so
you give the site what's true for the whole floor, and override it per cluster
where it differs:

| line | what to put in |
|---|---|
| `hardware_capex` + `lifetime_months` | purchase price of the servers, and how long you amortize them (48 is common). or a monthly `hardware_amortization` if your finance team already has that number. |
| `power_and_cooling` | monthly, the whole pool |
| `rack_and_network` | monthly — colo space, switches, uplinks |
| `licenses` | monthly — vmware, rhel, windows |
| `support`, `staff` | optional. staff is off by default, it's arguable. |

`weights` splits that total over cpu / ram / storage and must sum to 1.0.
`{cpu: 0.5, ram: 0.3, storage: 0.2}` is a reasonable start. every number clont
reports prints the weights that produced it, so a figure someone disputes can be
traced back.

**start rough, refine later.** the card sets the *scale* of a finding, not whether
it fires — a card that's 20% off still ranks your waste in the right order.

## 4. write clont.yaml

one file, the whole configuration. save this, replace the marked values:

```yaml
interval_seconds: 300
onprem:
  dc1:                                 # <- your site alias
    inventory:
      kind: vsphere
      endpoint: vc1.dc1.local          # <- hostname only, no https://
      username: clont-ro@vsphere.local # <- step 2
      password_env: DC1_VSPHERE_PASSWORD
      verify_ssl: true
      usage_window_days: 14            # how far back it reads perf counters
    rate_card:                         # <- step 3, monthly, whole site
      power_and_cooling: 3400
      rack_and_network: 1500
      licenses: 2000
      lifetime_months: 48
    weights: {cpu: 0.5, ram: 0.3, storage: 0.2}
    clusters:                          # optional, only where it differs
      prod-gen11:
        rate_card: {hardware_capex: 480000, licenses: 6000}
      dev-gen9:
        rate_card: {hardware_capex: 120000, lifetime_months: 60}
channels:
  log:
    min_severity: info                 # print everything to stdout for now
```

two notes:

- `usage_window_days: 0` prices everything but advises nothing: a vm clont
  couldn't measure is never called idle, because a missing measurement isn't the
  same as zero usage.
- a self-signed vcenter certificate needs `ca_bundle: /path/to/ca.pem`, **not**
  `verify_ssl: false`.

every key that exists is in [../clont.example.yaml](../clont.example.yaml).

## 5. optional: add your kubernetes clusters

skip this if you only want vm-level numbers.

a cluster is a *source*, not a provider — it has no cost of its own, its nodes
are vms you already pay for. so `priced_by` names the site, and it is required:

```yaml
kubernetes:
  lab:
    priced_by: dc1              # the site whose vms these nodes are
    in_cluster: true            # clont runs inside it; else kubeconfig: /etc/clont/kubeconfig
    match_by_name: true         # match nodes to vms by name when ids don't line up
    usage: metrics-server       # metrics-server | prometheus | off
```

a node clont can't place on priced iron is reported as **unmapped**, never priced
at zero, and the startup log says how many mapped and why the rest didn't. the
rbac it needs is `get`/`list` on six resources:
[iam.md](iam.md#kubernetes-get--list-nothing-else).

## 6. check it works, before deploying anything

```sh
export DC1_VSPHERE_PASSWORD='...'
CLONT_CONFIG=./clont.yaml clont run --once --summary -
```

one cycle, a summary to the screen, then it exits:

```
clont scan summary (2026-11-09T10:21:04+00:00, clont 0.3.2)
accounts:   dc1
duration:   22.7s

collected:
  metrics=0 costs=168 recommendations=4 health=12
...
estimated monthly savings:
  940.00 USD
```

read it in this order:

1. **is there an `errors:` block at the bottom?** that's the first thing to look
   at. wrong endpoint, bad credentials and tls problems all name themselves here.
2. **`costs=`** above 0 means vcenter was read and the card applied.
3. **the startup log lines about node mapping** (if you did step 5): `mapped 4/5
   nodes` and the reason for the one that didn't.
4. **do the monthly totals look like your real spend?** if the site total is far
   off, that's the rate card, not the agent — go back to step 3.

`--summary report.txt` writes the shareable report instead (dollar figure first),
`--summary out.json` the machine-readable one.

## 7. run it for real

**docker, on any linux box:**

```sh
docker run -d --name clont --restart unless-stopped \
  -v $PWD/clont.yaml:/etc/clont/clont.yaml:ro \
  -e DC1_VSPHERE_PASSWORD='...' \
  ghcr.io/clont-app/clont:0.3.2
docker logs -f clont
```

**kubernetes, with helm** — the chart isn't published to a registry yet, so
install from a checkout:

```sh
git clone https://github.com/clont-app/clont && cd clont
cp charts/clont/examples/values-onprem.yaml my-values.yaml
# edit my-values.yaml: the site alias, the rate card, the vcenter endpoint

kubectl create ns clont
kubectl -n clont create secret generic clont-vsphere --from-literal=password='...'
helm install clont ./charts/clont -n clont -f my-values.yaml
helm test clont -n clont       # checks the config the pod mounted, no api calls
kubectl -n clont logs deploy/clont -f
```

the password reaches the pod as an env var, and `rbac.create: true` is what lets
it read the cluster it runs in:

```yaml
rbac: {create: true}
env:
  - name: DC1_VSPHERE_PASSWORD
    valueFrom:
      secretKeyRef: {name: clont-vsphere, key: password}
```

all the chart values are in
[../charts/clont/README.md](../charts/clont/README.md).

## 8. notifications

the log channel only prints. add what you use — each channel has its own severity
floor:

```yaml
channels:
  slack:
    webhook_url: https://hooks.slack.com/services/XXX/YYY/ZZZ
    min_severity: warn
    repeat_hours: 24
```

a webhook comes from a slack app → incoming webhooks. **a config with a webhook
in it is a secret** — on kubernetes put the whole yaml in a Secret and point
`existingSecret` at it.

## 9. what to tune once it runs

- **`finops.onprem_min_savings_usd`** (1) — below this, a finding is noise.
- **`finops.onprem_idle_ram_pct`** (20) with **`idle_cpu_pct`** (5) — a vm must be
  quiet on *both* to be called idle.
- **`finops.onprem_rightsize_target_pct`** (70) — how much headroom to leave over
  the p95 when it suggests shrinking something.
- **`finops.k8s_unrequested_pct`** (50) — warn when that share of a cluster's
  priced iron isn't requested by any pod.
- **`finops.budgets`** — monthly ceilings work here too; `account:` takes a site
  alias.

## both aws and your own iron

one release handles both. `aws:`, `onprem:` and `kubernetes:` are independent
blocks in the same file: one cycle collects all of them, every message carries
its alias, and you get one digest and one set of budgets over the lot. a ready
example: [values-hybrid.yaml](../charts/clont/examples/values-hybrid.yaml).

each cluster's `priced_by` picks who pays for its nodes — a site under `onprem:`
for the lab, an account under `aws:` for eks.

you only need a second release when one pod can't reach both sides (vcenter not
routable from a cloud cluster), or the two estates belong to different teams. same
chart, another namespace, one config each.

## when it looks wrong

| what you see | usually |
|---|---|
| `ModuleNotFoundError: pyvmomi` | installed plain `clont`; use `clont[vsphere]` |
| tls error on connect | self-signed vcenter — point `ca_bundle` at the ca, don't disable verification |
| no onprem data at all | the site has a `rate_card` but no `inventory:` block — it's priced and idle, which is normal while the vcenter account is still being granted |
| no idle / rightsize findings | `usage_window_days: 0`, or vcenter doesn't retain perf counters that far back |
| site total under your real bill | the card is missing a cluster — you pay for the whole pool whether it's full or not, so every cluster needs covering |
| cluster priced, nodes unmapped | node names don't match the vm names: set `match_by_name: true` or fix the naming |
| storage counted twice | pvs/pvcs missing from the rbac — a cluster's own volumes are what nets off the site's storage gap |
| pod restarting right after start | a config error, fatal on purpose — the log names the key |
