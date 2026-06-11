# clont architecture

## What clont is

clont is a long-running, **read-only** monitoring + FinOps **agent**. It assumes
a read-only role in your cloud account(s), runs a `collect → detect → dispatch`
loop, and delivers decisions to chat (Slack / Discord / Telegram) — not
dashboards. It is **not** a metrics store, a rules engine, or an APM. It is a
decision/notification agent that gets more capable as you opt into higher tiers.

### Principles (hold across every tier)

- **Strictly read-only to the cloud.** The agent never writes to your cloud. All
  signal-gathering is `describe_*` / `Get*` / query calls.
- **Zero-setup, opinionated defaults.** The user should define as little as
  possible. Every forced threshold is an admission the agent isn't smart enough.
- **Chat-native.** Output is an explained event in your messenger, not a graph.
- **Multi-account, alias-keyed.** Accounts are a map keyed by a human alias
  (`prod`, `staging`); the alias flows into every event key and message.
- **Secrets stay on the agent.** Channel tokens and cloud credentials never leave
  the box — including in the paid tiers (see Local vs hosted).
- **FinOps + monitoring, unified.** One agent, one pipeline, both domains.

## The pipeline (one architecture, every tier)

```
collect / query (read-only)
   → detect: pluggable sink — local evaluator  OR  remote uploader
   → events (produced locally  OR  returned from clont cloud)
   → dispatch → channels (log / slack / discord / telegram)
```

Implemented today in:
- `clont/agent/runner.py` — the loop: per provider, run each registered
  collector, feed results through detectors, hand every event to every channel.
- `clont/core/registry.py` — collectors self-register by `(domain, cloud,
  service)`; the loop discovers them with no hard-coded imports.
- `clont/providers/` — read-only cloud auth (AWS: refreshable assume-role,
  multi-account, per-region clients).
- `clont/events/detectors.py` — turn collector output into `Event`s; the account
  alias is folded into the event **key** and **title**.
- `clont/channels/` — outbound delivery with per-channel severity gate +
  repeat/throttle.

**Design rule to protect:** events are **source-agnostic** and collectors stay
**dumb** (they gather; they don't decide). That single seam is what lets the same
`MetricPoint`/`HealthCheck` stream feed a local rule *or* a remote analyzer
without re-architecture.



