# estate-watch

External watchdog for the estate: a scheduled GitHub Actions workflow that probes the
public surfaces and the laptop heartbeat gist every 30 minutes **from GitHub's side**,
so an alert still fires when the laptop itself is dark (dead, asleep, or offline).

- UP = any HTTP 2xx/3xx/401/403 (Cloudflare Access/challenge gates count as reachable).
- Laptop heartbeat: a timestamp-only public gist pushed hourly by `estate-pulse.py
  --heartbeat` on the laptop. Stale > 2.5 h or unreachable ⇒ laptop dark.
- Alerting: a deduped `estate-incident` issue (opened after 2 consecutive failed
  probes, commented on recurrence, auto-closed on recovery). GitHub's native
  notifications carry it to phone/email — no extra apps.
- Steady green runs make zero writes.

Tuning lives in `.github/workflows/watch.yml` (probe list, `STALE_HOURS`, cadence).
