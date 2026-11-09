# quick start: own iron

zero to a running agent on your own vmware, with no aws anywhere. kubernetes
clusters on that iron join the same config. keys are in
[../clont.example.yaml](../clont.example.yaml), grants in [iam.md](iam.md).

**there is no invoice to read, so you write what a pool costs and clont divides
it by the capacity it measures.** the rate card is the one thing nobody can
derive for you — everything downstream (digest, spikes, forecast, budgets,
waste) is the same as on aws.

## 1. a read-only vcenter account

no custom role. make `clont-ro@vsphere.local` (or an ad user), give it the
built-in **Read-Only** role at the **vcenter root**, propagating to children.
that's the whole requirement — a standalone esxi host works the same way.
details and what it reads: [iam.md](iam.md#own-iron-a-vsphere-read-only-account).

the password never goes in argv: either inline in the yaml (mode 600) or
`password_env` naming the variable it arrives in. exactly one of the two.

## 2. the rate card

a pool is a **cluster**, not a site — a gen9 and a gen11 box don't cost the same
per core, and a site average hides the comparison you wanted. so the site card
holds what's true for the whole floor and a cluster card overrides it line by
line:

| line | what |
|---|---|
| `hardware_capex` + `lifetime_months` | or a monthly `hardware_amortization`, same line |
| `power_and_cooling` | monthly |
| `rack_and_network` | monthly |
| `licenses` | vmware / rhel / windows |
| `support`, `staff` | staff is off by default, it's arguable |

`weights` split the pool over cpu / ram / storage and must sum to 1.0. they're
printed next to every number they produced, so a disputed figure is traceable to
the split that made it.

**start rough.** the card decides the *scale* of every finding, not whether it
fires — a card that's 20% off still ranks the waste correctly. refine it once
the first report lands.

## 3. write the config

```yaml
interval_seconds: 300
onprem:
  dc1:
    inventory:
      kind: vsphere
      endpoint: vc1.dc1.local          # host only, no scheme
      username: clont-ro@vsphere.local
      password_env: DC1_VSPHERE_PASSWORD
      verify_ssl: true
      usage_window_days: 14            # 0 = no perf read, no idle/rightsize advice
    rate_card:
      power_and_cooling: 3400
      rack_and_network: 1500
      licenses: 2000
      lifetime_months: 48
    weights: {cpu: 0.5, ram: 0.3, storage: 0.2}
    clusters:
      prod-gen11:
        rate_card: {hardware_capex: 480000, licenses: 6000}
      dev-gen9:
        rate_card: {hardware_capex: 120000, lifetime_months: 60}
channels:
  log:
    min_severity: info
```

`usage_window_days: 0` prices everything but advises on nothing — a vm clont
couldn't measure is never called idle, because an absent measurement is not zero
usage. a self-signed vcenter cert wants `ca_bundle`, not `verify_ssl: false`.

## 4. add the kubernetes clusters

a cluster is a **source**, not a provider: `priced_by` names the site whose vms
its nodes are, and it's required.

```yaml
kubernetes:
  lab:
    priced_by: dc1
    in_cluster: true            # or kubeconfig: /etc/clont/kubeconfig
    match_by_name: true         # last resort: node name == vm name
    usage: metrics-server       # metrics-server | prometheus | off
```

a node clont can't place on priced iron is reported **unmapped**, never priced at
zero, and the startup log says how many nodes mapped and why the rest didn't. the
rbac is `get`/`list` on six resources: [iam.md](iam.md#kubernetes-get--list-nothing-else).

## 5. prove the read before deploying anything

```sh
pip install 'clont[vsphere,k8s]'     # the docker image already has both
export DC1_VSPHERE_PASSWORD=...
CLONT_CONFIG=./clont.yaml clont run --once --summary -
```

read three things in the summary: collectors that failed (wrong endpoint,
credentials, tls), how many nodes mapped per cluster, and whether the monthly
totals look like your actual spend. if the site total is far off, it's the card,
not the agent.

## 6. run it for real

helm, from [values-onprem.yaml](../charts/clont/examples/values-onprem.yaml):

```sh
kubectl create ns clont
kubectl -n clont create secret generic clont-vsphere --from-literal=password='...'
helm install clont oci://ghcr.io/clont-app/charts/clont -n clont -f values-onprem.yaml
helm test clont -n clont       # validates the mounted config, no api calls
```

the password arrives as env, `rbac.create: true` for the in-cluster read:

```yaml
rbac: {create: true}
env:
  - name: DC1_VSPHERE_PASSWORD
    valueFrom:
      secretKeyRef: {name: clont-vsphere, key: password}
```

the pod needs to reach vcenter on 443. details in
[../charts/clont/README.md](../charts/clont/README.md).

## 7. what to tune first

- `finops.onprem_min_savings_usd` (1) — below it a finding is noise.
- `finops.onprem_idle_ram_pct` (20) with `idle_cpu_pct` (5) — a vm has to be
  quiet on **both** to count as idle.
- `finops.onprem_rightsize_target_pct` (70) — headroom left over the p95 when
  shrinking. also the headroom for k8s workloads.
- `finops.onprem_unaccounted_min_gib` / `_min_pct` — both have to pass before
  unclaimed datastore space is reported.
- `finops.k8s_unrequested_pct` (50) — warn when that share of a cluster's priced
  iron no pod requested.
- `finops.budgets` — monthly ceilings work here too: `account:` takes a site
  alias.

## both estates in one release

`aws:`, `onprem:` and `kubernetes:` are independent maps. one cycle collects all
of them, every event carries its alias, and one digest and one set of budgets
cover the lot — see
[values-hybrid.yaml](../charts/clont/examples/values-hybrid.yaml). each cluster's
`priced_by` picks its payer: a site under `onprem:` for the lab, an account under
`aws:` for eks.

a second release only when one pod can't reach both sides (vcenter not routable
from the cloud cluster) or the two estates belong to different teams — same
chart, another namespace, one config each.

## when it looks wrong

| what you see | usually |
|---|---|
| no onprem data at all | the site has a `rate_card` but no `inventory` block — priced and idle, which is the normal state while the account is being granted |
| tls error on connect | self-signed vcenter — point `ca_bundle` at the ca, don't disable verification |
| no idle / rightsize findings | `usage_window_days: 0`, or the perf counters aren't retained for that window |
| site total under your real bill | the `headroom` line is missing — check the card covers every cluster, you pay for the pool whether it's full or not |
| cluster priced, nodes unmapped | node names don't match the vm names; set `match_by_name: true` or fix the naming |
| storage counted twice | pvs/pvcs not in the rbac — a cluster's own volumes are what nets off the site's gap |
| `ModuleNotFoundError: pyvmomi` | a plain `pip install clont` is aws-only, use `clont[vsphere]` |
