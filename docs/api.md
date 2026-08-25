# api uplink

the free tier is completely self-contained: clont reads your cloud with read-only
roles, works out the events locally and notifies your channels. nothing leaves
the agent except the notifications you asked for.

the **api uplink** (paid tier) adds a hosted clont server that runs the heavier
analytics we deliberately don't run on your box. you turn it on by adding an
`api:` block to `clont.yaml`:

```yaml
api:
  url: https://api.example.com/ingest
  api_key: "REDACTED"        # secret bearer token
  timeout_seconds: 10
```

no `api:` block, no uplink — the agent behaves exactly like the free tier.

## why it's two-way

hard rule: **your channel tokens never leave the agent box, even on paid tiers.**
which means the server can't notify your channels itself. so anything it works
out (a forecast, a recommendation, a cross-account anomaly) has to come **back**
to the agent, and the agent sends it through the channels it already owns. that's
why the uplink is request/response: the agent posts its batch, the server replies
with events to dispatch.

## the wire

one request per cycle:

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
  "events":          [ Event, ... ]          // what was detected locally this cycle
}
```

every record says which `cloud` and account `alias` it came from, so one batch
covers every configured account without splitting it up. money is sent as strings
(`Decimal`, so precision survives the trip) and timestamps are iso-8601.

the server replies with events to dispatch:

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

those go straight into the normal dispatch path, so they obey each channel's
severity gate and repeat throttle just like locally-made events. anything
malformed in the reply is logged and skipped — a bad response can never silence
the agent's own events.

## when it breaks

- the uplink is **best effort**: a network or http error gets logged and the cycle
  still dispatches the events it found locally. next cycle just tries again.
- traffic is **outbound https to your own server**. no cloud iam change, and it
  doesn't touch the read-only rule on cloud apis.
- `api_key` is a secret, so treat `clont.yaml` as sensitive (readable only by the
  agent's service account) — same as the webhook tokens under `channels:`.
