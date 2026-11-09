# changelog

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
