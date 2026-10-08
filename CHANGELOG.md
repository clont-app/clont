# changelog

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
