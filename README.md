# estate-watch

External watchdog for the estate: a scheduled GitHub Actions workflow that probes the
public surfaces and the laptop heartbeat gist every 30 minutes **from GitHub's side**,
so an alert still fires when the laptop itself is dark (dead, asleep, or offline).

- Redirects are followed and the terminal response is checked. Public surfaces require
  2xx. A declared Cloudflare Access surface must reach its exact Access host (or return
  an Access-specific response header); a generic origin/bot 401 or 403 is not green.
- Laptop heartbeat: a timestamp-only public gist pushed hourly by `estate-pulse.py
  --heartbeat` on the laptop. Stale > 2.5 h or unreachable ⇒ laptop dark.
- Alerting: a deduped `estate-incident` issue (opened after 2 consecutive failed
  probes, commented on recurrence, auto-closed on recovery). GitHub's native
  notifications carry it to phone/email — no extra apps.
- Steady green runs make zero writes.

Tuning lives in `.github/workflows/watch.yml` (probe list, `STALE_HOURS`, cadence).

This repository also owns the local, read-only drift audit. It is deliberately
separate from the reachability watchdog: a green HTTP probe does not prove that
instructions, copied skills, symlinks, repository splits, or registry semantics are
current.

```bash
python3 scripts/estate_drift.py                 # local checks only; no network
python3 scripts/estate_drift.py --network       # also record URL status/redirects
python3 -m unittest discover -s tests
```

The checker never writes or repairs anything. Its declarative inventory is
`config/estate-drift.json`. Expected-public, intentionally gated, intentionally
unpublished, and intentionally retired URLs are different states; a private GitHub
404 is not treated as proof that a repository is missing. Network/DNS status 0 is
`UNVERIFIED`, never evidence that a retired deployment is gone, and exits nonzero.
Installed global skill
copies and symlinks are fail-closed against `config/skill-provenance.json`; every
installed skill must have a versioned source, an exact symlink target, or a pinned
upstream tree digest.

`local-tools/projects-hygiene.sh` is the versioned source for the weekly local audit
wrapper. The scheduled wrapper may write its dated report and a desktop notification,
but it never files tasks or changes repositories: SQLite lint runs without `--failtask`
and the worktree janitor always runs with `--dry-run`. Synchronize/check the installed
copies with `scripts/sync-local-tools.sh --install|--check`.
