# clont architecture

## what clont is

a long-running, **read-only** monitoring + finops **agent**. it assumes a
read-only role in your cloud accounts, reads your own hypervisors and kubernetes
clusters the same way, runs a `collect → detect → dispatch` loop, and delivers
decisions to chat (slack / discord / telegram) — not dashboards.

it is **not** a metrics store, a rules engine or an apm. it's a thing that
notices something and tells you, and it gets smarter as you opt into higher tiers.

### principles (true at every tier)

- **strictly read-only to the cloud.** the agent never writes. everything is
  `describe_*` / `Get*` / a query.
- **sane defaults, minimal setup.** you should have to define as little as
  possible. every threshold we force on you is an admission the agent isn't clever
  enough yet.
- **chat-native.** output is an explained event in your messenger, not a graph.
- **many accounts, keyed by alias.** accounts are a map keyed by a human name
  (`prod`, `staging`); that alias flows into every event key and message.
- **secrets stay on the agent.** channel tokens and cloud credentials never leave
  the box, paid tiers included.
- **finops and monitoring together.** one agent, one pipeline, both jobs.
- **never guess a price.** a cloud has an invoice, so clont reads it; a
  datacenter doesn't, so the operator writes a rate card and clont divides it by
  the capacity it measured. the first `if model == ...: price = ...` would be a
  bug.

## the pipeline (same shape at every tier)

```
collect / query (read-only)
   → detect: pluggable sink — local evaluator  OR  remote uploader
   → events (made locally  OR  returned from clont cloud)
   → dispatch → channels (log / slack / discord / telegram)
```

where that lives today:

- `clont/agent/runner.py` — the loop: for each provider, run every registered
  collector, push the results through the detectors, hand each event to every
  channel.
- `clont/core/registry.py` — collectors register themselves by
  `(domain, cloud, service)` and the loop finds them. no hard-coded imports, so a
  new collector is one decorated class (`public_ipv4` was exactly that).
- `clont/providers/` — read-only auth and the parsing helpers collectors share.
  `aws/` is refreshable assume-role, multi-account, per-region clients;
  `onprem/` is one vcenter pass (the wire in `vsphere.py`, the arithmetic in
  `inventory.py`, perf counters in `metrics.py`) kept free of vsphere types so
  proxmox / libvirt can fill the same dicts later; `k8s/` is nodes, pods, pvcs
  and a usage source (metrics-server or prometheus).
- `clont/finops/onprem/` — the rate card (`rates.py`, `config.py`), the cost
  records (`costs.py`) and the waste pass (`waste.py`). **a pool is a cluster**:
  hardware generations and licensing differ per cluster, and a site average hides
  the comparison the report exists for.
- `clont/finops/k8s/` — a cluster is a **source**, not a provider. `priced_by`
  points at a pool that is already priced, and the chain is
  `pool card → node vm cost record → node → namespace requests`. nothing is
  redistributed and no node price is invented: a node that can't be placed comes
  back `unmapped`, never zero.
- `clont/events/detectors.py` — collector output becomes `Event`s; the account
  alias goes into the event **key** and **title**.
- `clont/channels/` — delivery, with a per-channel severity gate and repeat
  throttle.
- `clont/reporting/summary.py` — the ad-hoc read path over one cycle's `Batch`
  (`clont run --summary`), rendered as text or json. it summarizes what the loop
  already collected; it never collects anything itself.

## the seam worth protecting

events are **source-agnostic** and collectors stay **dumb** — they gather, they
don't decide. that single seam is what lets the same `MetricPoint` / `HealthCheck`
stream feed a local rule *or* a remote analyzer without rewriting anything.

a second rule falls out of it, learned the hard way on the public ipv4 collector:
**cost records and recommendations are not the same thing.** a collector reports
what everything costs; only the clear, deterministic waste becomes a
recommendation. mixing the two turns the savings number into noise and teaches
people to ignore the agent.

and a third, from the on-prem and k8s work: **spend is what was reserved,
advice is what was measured.** a cost record bills the vm or the pod request the
operator actually pays for, so the site total matches the invoice; the p95 rides
along as dimensions, and only ever produces advice. an absent measurement means
no advice — never zero usage. every finding also says whether it is exact (a
cluster's own rate card) or `approximate` (a mean rate, or capacity handed back
that the invoice only follows once a node goes away).
