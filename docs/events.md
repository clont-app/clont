# how events work

every cycle walks the same path:

```
collect (read-only) ──► detect ──► events ──► send to channels
```

1. **collect.** read cost, metrics and health from each cloud.
2. **detect.** turn that into **events** — an idle resource, a failing check, a
   spend spike. each event has a severity (`info` / `warn` / `critical`) and a
   stable **key** naming the *condition*, not the moment
   (`monitoring:health:prod:aws:ec2:i-123` — `prod` is the account alias, so two
   accounts never collide). the key is how clont recognises the same problem next
   cycle.
3. **dispatch.** hand every event to every channel. each channel decides for
   itself with two knobs:
   - **`min_severity`** — ignore anything below this.
   - **`repeat_after`** — after firing for a key, stay quiet this long. `none`
     means fire once and never again.

## when a channel actually fires

same rule everywhere, only the defaults differ:

- **log** (always on) — floor `info`, re-logs a standing problem every ~3h.
- **slack / discord / telegram** — floor `warn`, fire once, repeat only if you
  set `repeat_hours`.

so a brand new `critical` hits the log and every notifier at once. while it's
still open the log keeps a throttled record and the notifiers stay quiet, unless
you gave them a `repeat_hours` to nag you. one condition, log repeat 3h, slack
`repeat_hours=24`, telegram once-only:

```
t=0h    new        → log ✓  slack ✓  telegram ✓
t=0h05  still open → log –  slack –  telegram –     (everyone's in their window)
t=3h    still open → log ✓  slack –  telegram –     (log window elapsed)
t=24h   still open → log ✓  slack ✓  telegram –     (slack nags again)
```

channels live outside the clouds they report on and use their own credentials,
kept away from the read-only cloud role.
