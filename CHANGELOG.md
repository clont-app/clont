# changelog

## 0.3.4

### docs

- "before you install" steps in the readme: cur bucket and `clont-readonly` role for
  aws, vcenter user and rate card for on-prem
- `docs/setup-cloud.md` reordered: bucket and export first, then the role

### helm chart

- chart and appVersion move to 0.3.4, so the default image tag exists in ghcr
  (0.3.2 had no image)

## 0.3.3

### agent spend

- per-agent spend report off cur — tokens, cache and fixed floors per agent, named by
  `finops.agent_tag_keys`; empty list = off
- agent spend detector — one event per agent and account, WARN when fixed floors eat
  `agent_floor_pct` of the bill, plus a finding for uncached input tokens
- idle agent stack — a floor that kept billing with no token traffic for
  `agent_idle_days`
- tested on real cur 2.0 rows from a bedrock nova micro run

### cur

- reads cur 2.0 exports
- finds the cur export by itself, no bucket/prefix in the config needed

## 0.3.2

### helm chart

- `charts/clont` — deployment, configmap, serviceaccount, optional rbac for the
  in-cluster kubernetes source, and a `helm test` hook that validates the config
- three example values files: `values-cloud.yaml`, `values-onprem.yaml`,
  `values-hybrid.yaml` (aws and your own iron in one release)

### docs

- `docs/setup-cloud.md` and `docs/setup-onprem.md` — step-by-step setup from an
  empty account to a running deployment, written for a first-time install

### fixes

- `clont run --once --summary` counted accounts from `aws` only, so an on-prem-only
  scan reported "no accounts"; sites are counted too now

## 0.3.1

### compute optimizer

- an access denial is no longer reported as "not enrolled" — `AccessDenied*` logs a
  WARNING naming the missing grant, `OptInRequiredException` stays INFO
- lambda and rds recommendations need a grant on the underlying service:
  `lambda:ListProvisionedConcurrencyConfigs`, `rds:DescribeDBClusters`, both added to
  the read-only policy in `docs/iam.md`
- preflight probes the lambda and rds recommendation calls, so a missing grant shows
  up before a run instead of silently returning nothing

## 0.3.0

### vsphere / on-prem

- vsphere provider — one inventory and capacity pass over vCenter or a standalone ESXi host
- on-prem rate card, allocated per site and per cluster to cpu, memory and disk
- cost records and 8 kinds of priced waste finding from that same pass
- measured usage from vCenter perf counters, so idle and rightsize use p95, not configured size

### kubernetes on-prem

- nodes mapped onto the priced node pools they run on
- namespace showback over the priced pool
- workload rightsizing against measured usage
- node-pool and pvc findings, with pvc capacity netted against the datastore it occupies

Earlier releases: see the git history.
