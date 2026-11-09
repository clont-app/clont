# clont helm chart

runs the agent as a single-replica Deployment: config from a ConfigMap (or a
Secret you manage), an optional read-only ClusterRole for `kubernetes:` sources,
and nothing listening on a port.

```sh
helm install clont oci://ghcr.io/clont-app/charts/clont --version 0.3.1 \
  -n clont --create-namespace -f my-values.yaml
```

from a checkout:

```sh
helm install clont ./charts/clont -n clont --create-namespace -f my-values.yaml
helm test clont -n clont          # validates the mounted config, no api calls
```

## quick start

three ready values files in [examples/](examples/) — copy one, change the
aliases, the account ids and the rate card, install.

### cloud (aws)

[`values-cloud.yaml`](examples/values-cloud.yaml) — two accounts, spend from the
cost and usage report, budgets and tag hygiene. identity is irsa on eks
(`serviceAccount.annotations`), or `envFrom` a secret with access keys anywhere
else; the role's policy is in [docs/iam.md](../../docs/iam.md). `rbac.create` is
off, nothing reads the cluster.

```sh
helm install clont oci://ghcr.io/clont-app/charts/clont -n clont \
  --create-namespace -f values-cloud.yaml
```

### on-prem (no aws at all)

[`values-onprem.yaml`](examples/values-onprem.yaml) — a vsphere site plus the
cluster clont runs in. there is no invoice to read, so you write the monthly cost
of the pool (`rate_card`) and clont divides it by the capacity it measures in
vcenter; everything downstream — digest, spikes, forecast, budgets, waste — is
the same as on aws. needs the vcenter password as a secret and
`rbac.create: true` for the in-cluster read.

```sh
kubectl create ns clont
kubectl -n clont create secret generic clont-vsphere --from-literal=password='...'
helm install clont oci://ghcr.io/clont-app/charts/clont -n clont -f values-onprem.yaml
```

### both

[`values-hybrid.yaml`](examples/values-hybrid.yaml) — **one release.** `aws:`,
`onprem:` and `kubernetes:` are independent maps, one cycle collects all of them
and every event carries its alias. each cluster's `priced_by` names the pool that
pays for its nodes: a site under `onprem:` for the lab, an account under `aws:`
for eks, so a mixed estate reports in one digest with one set of budgets.

a second release is only for when one pod cannot reach both sides (vcenter not
routable from the cloud cluster) or the two belong to different teams — same
chart, another namespace, one config each.

## config

the agent's whole configuration is `clont.yaml` — the keys are documented in
[clont.example.yaml](../../clont.example.yaml), and the chart renames none of
them. three ways in, pick one:

| values | where the file ends up |
|---|---|
| `config:` (default) | a ConfigMap the chart owns; a change restarts the pod |
| `existingConfigMap: name` | your ConfigMap, key `clont.yaml` |
| `existingSecret: name` | your Secret, key `clont.yaml` |

**secrets go in a Secret.** slack/discord webhooks, a telegram token and
`api.api_key` live inside that yaml, so a config carrying them belongs in
`existingSecret`, not in `values.yaml`. a vsphere password is the exception —
`inventory.password_env` names an env var, which `env:` fills from a secretKeyRef.

```yaml
env:
  - name: DC1_VSPHERE_PASSWORD
    valueFrom:
      secretKeyRef: {name: clont-vsphere, key: password}
```

## aws access

on eks give the service account irsa and let clont assume the per-account roles:

```yaml
serviceAccount:
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::111111111111:role/clont
```

anywhere else, feed credentials the usual way (`envFrom` a secret with
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`). the policy to attach is in
[docs/iam.md](../../docs/iam.md).

## reading the cluster it runs in

```yaml
rbac: {create: true}            # get/list on nodes, pods, namespaces, pvcs, pvs, metrics
config:
  kubernetes:
    lab:
      priced_by: dc1            # required: the site or account that pays for the nodes
      in_cluster: true
```

`rbac.create: false` when clont only watches aws or vmware — the role buys
nothing then.

## notes

- **one replica, `strategy: Recreate`** — two agents would collect the same
  accounts twice and notify twice on one condition.
- **no probes.** the daemon serves no port; a liveness check would have to be
  `clont run`'s own log. the container exits on a fatal config error, which the
  restart count shows.
- **read-only root filesystem**, non-root uid 10001, all capabilities dropped.
  `/var/lib/clont` and `/tmp` are emptyDirs.
- out of the box clont makes no billed api call; `finops.allow_cost_explorer`
  and `allow_cloudwatch_metrics` are the two that cost money.

## values

| key | default | what |
|---|---|---|
| `image.repository` | `ghcr.io/clont-app/clont` | also on docker hub as `clont/clont` |
| `image.tag` | `""` | empty = chart appVersion |
| `config` | minimal, log channel only | the clont.yaml body |
| `existingConfigMap` / `existingSecret` | `""` | use your own config object |
| `configKey` | `clont.yaml` | key inside that object |
| `serviceAccount.create` / `.name` / `.annotations` | `true` / `""` / `{}` | irsa goes in the annotations |
| `rbac.create` | `true` | read-only ClusterRole + binding |
| `env` / `envFrom` | `[]` | extra env, e.g. a vsphere password |
| `args` | `["run"]` | the daemon |
| `resources` | 50m/128Mi, limit 512Mi | |
| `extraVolumes` / `extraVolumeMounts` | `[]` | e.g. a private ca bundle |
