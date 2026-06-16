# API uplink

The free tier is fully self-contained: clont collects from your read-only cloud
roles, detects events locally, and notifies your channels — nothing leaves the
agent except the notifications you configure.

The **API uplink** (paid tier) adds a hosted clont server that runs heavier,
server-side analytics the agent deliberately doesn't run on-box. It is enabled
purely by adding an `api:` block to `clont.yaml`:

```yaml
api:
  url: https://api.example.com/ingest
  api_key: "REDACTED"        # secret bearer token
  timeout_seconds: 10
```

With no `api:` block the uplink is inert and the agent behaves exactly as the
free tier.

## Why it's two-way

A hard rule: **channel tokens (Slack/Discord/Telegram) never leave the agent
box, even in paid tiers.** The server therefore can never notify your channels
itself — so anything it computes (a forecast, a recommendation, a cross-account
anomaly) has to come **back** to the agent, which dispatches it through the
channels it already owns. The uplink is consequently request/response: the agent
POSTs its batch and the server replies with events to dispatch.

## Wire contract

Each cycle the agent makes a single request:

```
POST {url}
Authorization: Bearer <api_key>
Content-Type: application/json

{
  "agent": "clont",
  "metrics":         [ MetricPoint, ... ],
  "costs":           [ CostRecord, ... ],
  "recommendations": [ Recommendation, ... ],
  "health":          [ HealthCheck, ... ],
  "events":          [ Event, ... ]          // the events detected locally this cycle
}
```

Each record self-identifies by `cloud` and account `alias`, so one batch can
span every configured account without a per-account split. `Decimal` money
values are sent as strings (to preserve precision); timestamps are ISO-8601.

The server replies with the events to dispatch locally:

```
200 OK
Content-Type: application/json

{
  "events": [
    { "key": "...", "severity": "warn|info|critical", "domain": "monitoring|finops",
      "cloud": "aws", "title": "...", "message": "...",
      "resource": { "cloud", "service", "resource_id", "region?", "alias?" },   // optional
      "payload": { ... },                                                        // optional
      "timestamp": "2026-01-01T00:00:00+00:00" }                                 // optional
  ]
}
```

Returned events are fed straight into the normal dispatch path, so they obey
each channel's severity gate and repeat throttle just like locally-detected
events. Malformed items in the reply are logged and skipped — a bad response can
never silence the agent's own events.

## Failure & safety

- The uplink is **best-effort**: a network/HTTP error is logged and the cycle
  still dispatches its locally-detected events. The next cycle retries naturally.
- Traffic is **outbound HTTPS to your own server** — it needs no cloud IAM
  change and doesn't touch the read-only invariant on cloud APIs.
- `api_key` is a secret; treat `clont.yaml` as sensitive (readable only by the
  agent's service account), the same as the webhook tokens under `channels:`.
