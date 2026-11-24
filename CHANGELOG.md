# changelog

## unreleased

### upgrading

- optional: `Datastore.Browse` on the vsphere role turns on datastore gc. without
  it clont warns once per run and `unaccounted-storage` stays a size, see
  [docs/setup-onprem.md](docs/setup-onprem.md)

### finops

- on-prem: `old-vm-snapshot` (snapshots older than `onprem_snapshot_max_days`,
  default 3, sized off `layoutEx`, backup leftovers named) and
  `consolidation-needed` ($0, a risk). still under the Read-Only role
- on-prem datastore gc: `orphaned-vmdk` and `unregistered-vm`, from
  `layoutEx` edges against the datastore file list. fresh files
  (`onprem_gc_min_age_days`, 7), `onprem_gc_keep` paths, `.fcd` / csi dirs,
  isos and datastores mounted outside the pass are live. what gc lists comes off
  `unaccounted-storage`, and a datastore that fails the parity check drops its rows
- a vm with no `layoutEx` makes only its own datastores all-live, not the whole site
- a datastore whose free space lags its vms (hostd after a power-on) is all-live with
  a refresh hint, not a parity failure
- two or more orphaned disks under the savings floor on one datastore fold into one
  `small-orphans` row
- `inventory.browse` (default `true`) turns the browse off per site
- preflight checks `Datastore.Browse` with `HasPrivilegeOnEntities` once per run;
  a datastore refused once is not browsed again that run
- datastore browse paths come out in the `layoutEx` form whatever the host
  answers (`[ds]/vm` from vcsim, `[ds] vm/` from esxi)
- on-prem `cns-leftover` (vcenter only, `Cns.Searchable`): csi volumes whose cluster
  clont reads and holds no pv for them, cluster ids learned or mapped in
  `inventory.cns_clusters`
- `template-disk` on a vcenter says when the template was last cloned, or that it
  wasn't over the event history vcenter still holds. no verdict on esxi or a failed read

### fixes

- on-prem: a running vm whose `layoutEx` is over `onprem_layout_max_lag_minutes` (10)
  older than its datastore's free space no longer reads as `unaccounted-storage`, and
  gc treats that datastore as all-live

### tests

- `tests/test_vsphere_vcsim.py`, opt-in (`CLONT_VCSIM=1`): the real vsphere code
  against vcsim — snapshot tree, `layoutEx`, the browse, gc end to end and the
  read-only call list, which now includes `SearchDatastoreSubFolders_Task`

## 0.4.1

### finops

- aws backup snapshots (`aws:backup:source-resource`) are no longer reported as
  stale or orphaned, in `snapshots` and eks leftovers. the vault owns them, same
  as dlm

## 0.4.0

### upgrading

- widen the read-only role first: `ec2:DescribeImages`,
  `ec2:DescribeImageAttribute`, `ec2:DescribeSnapshotAttribute`,
  `ec2:DescribeLaunchTemplates`, `ec2:DescribeLaunchTemplateVersions`,
  `autoscaling:DescribeLaunchConfigurations`, optional `backup:ListBackupVaults`
  + `backup:ListRecoveryPointsByBackupVault`, and `ssm:DescribeParameters` +
  `ssm:GetParameters` (`ClontAmiAliases`). see [docs/iam.md](docs/iam.md).
  preflight probes `ec2:DescribeImages`, `ec2:DescribeLaunchTemplates` and
  `autoscaling:DescribeLaunchConfigurations`; any other missing grant shows up
  as a WARNING in the run log naming it. a missing required grant makes the
  region report no orphans, it never guesses

### finops

- orphaned amis: mark and sweep from instances, launch templates and launch
  configurations. an orphan ami and the snapshots only it holds are one
  finding, and the finding says why it's an orphan
- dlm, aws backup, shared amis, ami ids in ssm parameters, `clont:keep` and
  `finops.ami_keep` are roots. a snapshot that is shared, public or used by a
  launch template is kept, never on the delete list.
  `ami_unlaunched_days` (120) and `ami_min_age_days` (30) set the window
- eks leftovers: volumes, csi snapshots, addresses, and load balancers a
  deleted cluster tagged `owned`, one finding per cluster. a running instance
  that still carries the tag keeps the cluster alive. clusters are listed in
  every enabled region, so an explicit `regions:` list can't make a live one
  look gone
- stale snapshots: one that backs an ami or is dlm-owned is no longer
  reported. behind an orphan ami it is part of that ami's finding

### helm chart

- chart and appVersion move to 0.4.0

## 0.3.8

### alerts

- `channels.state_file` keeps last-sent times on disk, so a restart no longer
  re-alerts every open condition. unset = memory only, as before
- helm chart sets it to `/var/lib/clont/alert-state.json`; `persistence.enabled`
  adds a pvc so it survives a pod reschedule too, `fsGroup: 10001` lets the pod
  write to it

### docs

- python 3.14 in the install docs
- `SECURITY.md` and `CONTRIBUTING.md`

### helm chart

- chart and appVersion move to 0.3.8

## 0.3.7

### finops

- forecast no longer dips on a half-loaded cur day: the last day is left out of
  the average and counted as max(billed so far, usual cost for that weekday)
- the same cur warning is logged once, not on every pass

### docs

- `cloudwatch:GetMetricData` moves into the main iam policy
- `clont.example.yaml` marks `allow_cloudwatch_metrics` as paid, with the price

### helm chart

- chart and appVersion move to 0.3.7

## 0.3.6

### finops

- idle elasticache: a cache or replication group that served no get/set commands
  over the window (redis, valkey, memcached)
- idle dynamodb: a table with no consumed reads or writes, gsi reads included;
  tables younger than the window are skipped
- both ride `finops.allow_cloudwatch_metrics`, like idle rds
- `clont.example.yaml` turns `allow_cloudwatch_metrics` on

### docs

- iam the cur setup script needs: `cur:*`, `bcm-data-exports:*`, s3 bucket rights

### helm chart

- chart and appVersion move to 0.3.6

## 0.3.5

### cur

- the role needs `s3:ListBucket` beside `s3:GetObject`, else a missing manifest reads
  as AccessDenied; the setup notice and `docs/iam.md` say so

### docs

- `docs/setup-cloud.md`: cur 2.0 export and bucket policy via aws cli, marked aws only
- clont never creates the export, it is made once by hand; bucket names are examples
- irsa trust policy for eks

### helm chart

- chart and appVersion move to 0.3.5

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
