#!/usr/bin/env python3
"""estate-pulse — the 4x/day pulse checks that ride inside fleet-sentinel.

NOT a second sentinel: this is a module invoked by fleet-sentinel.sh's run. It
adds three event-time checks the daily hygiene pass did not cover, because the
2026-07-23 triage found three lanes that stopped silently overnight and nothing
noticed or relaunched them:

  CHECK A  launchd exits   — every loaded com.jeff(pinto).* job with last-exit != 0
                             while not running -> one rich, phone-readable failtask
                             card per job (label, code, stderr tail, kickstart cmd).
  CHECK B  loop liveness   — config-driven long-running loops (lantern's two
                             backfills). A loop with work remaining that has died
                             or stopped progressing is RELAUNCHED via the project's
                             OWN idempotent resume path, once, then escalated to an
                             error card if the relaunch does not take. No storms.
  CHECK C  stale PRs       — open PRs idle > 18h across the active repos -> one
                             rollup card. One gh call per repo (quota-polite).

Cost discipline: pure shell / sqlite / gh. NO claude/LLM calls — it runs 4x/day
forever and must cost nothing (usage-limit guardrail).

Delivery is failtask cards only (they land on Jeff's board = his phone). No email,
no new channels. All state is written atomically. --dry-run prints what WOULD fire.
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
FAILDIR = os.path.join(HOME, ".claude", "failures")
CONFIG = os.path.join(FAILDIR, "sentinel-config.json")
STATE = os.path.join(FAILDIR, "pulse-state.json")
PULSE_LOG = os.path.join(FAILDIR, "pulse.log")
FAILTASK = os.path.join(HOME, ".claude", "bin", "failtask")

DRYRUN = "--dry-run" in sys.argv[1:]
UID = os.getuid()
FAILURES_JSONL = os.path.join(FAILDIR, "failures.jsonl")
HEARTBEAT = os.path.join(FAILDIR, "heartbeat.json")
HEARTBEAT_MODE = "--heartbeat" in sys.argv[1:]


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def plog(msg):
    line = "%s %s%s" % (now_iso(), "[dry-run] " if DRYRUN else "", msg)
    try:
        with open(PULSE_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print("estate-pulse: " + msg)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_atomic(path, obj):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def find_gh():
    for c in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh"):
        if os.access(c, os.X_OK):
            return c
    from shutil import which
    return which("gh")


def file_card(project, title, body, key, severity):
    """File a failtask card (or print in dry-run). failtask itself dedupes on key."""
    if DRYRUN:
        plog("WOULD FILE [%s] %s (key=%s sev=%s)" % (project, title, key, severity))
        return
    try:
        subprocess.run(
            [FAILTASK, project, title, "--detail", body,
             "--dedupe-key", key, "--severity", severity],
            timeout=150, check=False)
        plog("FILED [%s] %s (key=%s sev=%s)" % (project, title, key, severity))
    except Exception as e:
        plog("FILE FAILED [%s] %s: %r" % (project, title, e))


def proj_for_label(label):
    table = [
        ("com.jeff.mlb-", "mlb-hits-forecast"),
        ("com.jeffpinto.lantern-", "lantern"),
        ("com.jeffpinto.atlas-", "music-atlas"),
        ("com.jeffpinto.privacy-judge-", "privacy-judge"),
        ("com.jeffpinto.site-dev", "jeffpinto-site"),
        ("com.jeffpinto.common-carrier-dev", "common-carrier"),
        ("com.jeffpinto.bluecamel-tunnel", "bluecamel"),
        ("com.jeffpinto.notes-vault-", "notes-vault"),
        ("com.jeffpinto.rvc-posture-sweep", "rvc-homeowner-taxes"),
        ("com.jeffpinto.outbound-crm", "outbound"),
        # Exact label, not a "com.jeffpinto.outbound-" prefix: outbound-crm
        # already has its own row and a prefix rule would shadow it.
        ("com.jeffpinto.outbound-mint", "outbound"),
        ("com.jeffpinto.crm-backup", "outbound"),
        ("com.jeffpinto.fleet-sentinel", "fleet-sentinel"),
        ("com.jeffpinto.gh-board-export", "gh-board"),
        ("com.jeffpinto.prelaunch-bundle", "bundle"),
        ("com.jeffpinto.worklog-nightly", "infra"),
        ("com.jeffpinto.projects-hygiene", "infra"),
    ]
    for prefix, proj in table:
        if label.startswith(prefix):
            return proj
    return "infra"


def plist_info(label):
    """Return (stderr_path, keepalive_bool) for a launchd label, best-effort."""
    plist = os.path.join(HOME, "Library", "LaunchAgents", label + ".plist")
    try:
        import plistlib
        with open(plist, "rb") as f:
            d = plistlib.load(f)
        serr = d.get("StandardErrorPath") or d.get("StandardOutPath") or ""
        ka = d.get("KeepAlive")
        keepalive = bool(ka) if not isinstance(ka, dict) else True
        return serr, keepalive
    except Exception:
        return "", False


def tail(path, n=10):
    try:
        with open(path, "r", errors="replace") as f:
            return "".join(f.readlines()[-n:]).rstrip("\n")
    except Exception:
        return ""


ACKS = os.path.join(FAILDIR, "acks.json")


def ack_active(label, status, serr):
    """An ack (written by ~/.claude/bin/failack) suppresses a launchd finding only while
    ALL of these hold: same exit code, not expired, and the job has not run since the ack
    (its log has not advanced). A genuinely new failure is therefore always loud."""
    try:
        with open(ACKS) as fh:
            ack = json.load(fh).get(label)
    except Exception:
        return None
    if not ack or str(ack.get("exit_code")) != str(status):
        return None
    if time.time() >= ack.get("expires_ts", 0):
        return None
    was = ack.get("log_mtime_at_ack")
    if was and serr and os.path.isfile(serr) and os.path.getmtime(serr) > was + 1:
        return None   # job ran again and failed again — that is news, not the acked one
    return ack


# ---------------------------------------------------------------- CHECK A
def check_launchd_exits(cfg):
    ignore = set(cfg.get("launchd_ignore", []))
    try:
        out = subprocess.run(["/bin/launchctl", "list"], capture_output=True,
                             text=True, timeout=30).stdout
    except Exception as e:
        plog("CHECK A: launchctl list failed: %r" % e)
        return
    n = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        pid, status, label = parts[0], parts[1], parts[2]
        if not (label.startswith("com.jeffpinto.") or label.startswith("com.jeff.")):
            continue
        if label in ignore:
            continue
        # not running (pid == '-') AND last exit nonzero
        if pid != "-" or status == "0":
            continue
        try:
            code = int(status)
        except ValueError:
            continue
        serr, keepalive = plist_info(label)
        # Positive exit codes are genuine failures (script broke: 127, 1, 2 ...) —
        # always flag. Negative codes are signal terminations (SIGTERM=-15,
        # SIGKILL=-9): for a NON-keepalive one-shot (a RunAtLoad boot job like
        # lantern-resume-jobs) that means a deliberate stop/restart, not a silent
        # failure, so skip it. For a KeepAlive service, a signal-kill while NOT
        # running means launchd gave up on something meant to stay up -> flag.
        if code < 0 and not keepalive:
            plog("CHECK A: skip %s (signal exit %d, non-keepalive one-shot)" % (label, code))
            continue
        acked = ack_active(label, status, serr)
        if acked:
            plog("CHECK A: acked %s (exit %s, %.1fh left) — %s"
                 % (label, status, (acked["expires_ts"] - time.time()) / 3600.0,
                    acked.get("note", "")))
            continue
        n += 1
        if serr and os.path.isfile(serr):
            # Say how old the log is. An unrotated stderr file accumulates months of
            # history, so its tail reads as tonight's failure when it may be from April
            # (2026-07-24: mlb's launchd.err held 3 months of ghosts and cost a sweep
            # real time re-diagnosing already-fixed bugs).
            age_h = (time.time() - os.path.getmtime(serr)) / 3600.0
            ttxt = ("[log last written %.1fh ago — lines above may predate this failure]\n%s"
                    % (age_h, tail(serr, 10)))
        else:
            ttxt = "(no StandardErrorPath log found: %s)" % (serr or "none")
        body = (
            "label: %s\n"
            "exit code: %s (not currently running)\n\n"
            "stderr tail (%s):\n%s\n\n"
            "RESTART (copy-paste on the Mac):\n"
            "  launchctl kickstart -k gui/%d/%s\n\n"
            "Filed by estate-pulse / fleet-sentinel (4x/day). Recurs deduped while this\n"
            "card is open or Done-and-reopened (key launchd:%s). The job's own failtask\n"
            "call shares this key, so one failure files one card. If the job is\n"
            "meant to be manual/down, add %s to launchd_ignore in sentinel-config.json."
            % (label, status, serr or "none", ttxt, UID, label, label, label)
        )
        file_card(proj_for_label(label), "launchd job %s exited %s" % (label, status),
                  body, "launchd:%s" % label, "error")
    plog("CHECK A: %d launchd job(s) down with nonzero exit" % n)


# ---------------------------------------------------------------- CHECK B
def loop_remaining(progress_cmd):
    """Run the loop's progress_cmd; stdout must be an integer = remaining work.
    remaining == 0 means COMPLETE / healthy-idle (never a stall)."""
    try:
        r = subprocess.run(["/bin/bash", "-c", progress_cmd],
                           capture_output=True, text=True, timeout=60)
        return int((r.stdout or "0").strip() or "0")
    except Exception as e:
        plog("  progress_cmd failed (%r) -> treating remaining as unknown" % e)
        return None


def loop_alive(lockdir):
    """A loop is alive iff its lockdir exists and the pid inside is live."""
    pidf = os.path.join(lockdir, "pid")
    if not os.path.isdir(lockdir) or not os.path.isfile(pidf):
        return False, None
    try:
        pid = int(open(pidf).read().strip())
    except Exception:
        return False, None
    try:
        os.kill(pid, 0)
        return True, pid
    except Exception:
        return False, pid


def do_relaunch(loop, relaunched_this_run):
    """Relaunch a loop via its project's own idempotent resume path. Clears a
    STALE lockdir first (only when the pid inside is dead). Deduped per run so
    two stalled loops sharing one resume_cmd don't fire it twice."""
    lockdir = os.path.expanduser(loop["lockdir"])
    alive, pid = loop_alive(lockdir)
    if not alive and os.path.isdir(lockdir):
        if DRYRUN:
            plog("  WOULD clear stale lockdir %s (pid %s dead)" % (lockdir, pid))
        else:
            import shutil
            try:
                shutil.rmtree(lockdir)
                plog("  cleared stale lockdir %s (pid %s dead)" % (lockdir, pid))
            except Exception as e:
                plog("  lockdir clear failed: %r" % e)
    cmd = loop["relaunch_cmd"]
    if cmd in relaunched_this_run:
        plog("  relaunch already fired this run for shared resume path — skip")
        return
    relaunched_this_run.add(cmd)
    if DRYRUN:
        plog("  WOULD relaunch: %s" % cmd)
        return
    try:
        subprocess.Popen(["/bin/bash", "-c", cmd], start_new_session=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        plog("  relaunched: %s" % cmd)
    except Exception as e:
        plog("  relaunch FAILED: %r" % e)


def check_loops(cfg, state):
    loops = cfg.get("pulse_loops", [])
    lstate = state.setdefault("loops", {})
    relaunched_this_run = set()
    for loop in loops:
        name = loop["name"]
        proj = loop.get("project", "infra")
        if loop.get("paused"):
            # A paused loop is a ruling, not a stall: never relaunch it and never
            # file a card for it. (2026-09-10: the lantern lab was paused by Jeff
            # at 06:10 and the 08:01 pulse relaunched resume-jobs.sh anyway,
            # pulling a 16 GB model onto a Mac with 0.1 GB free.)
            plog("CHECK B [%s]: PAUSED — no relaunch, no card (%s)"
                 % (name, loop.get("_pause_reason", "no reason recorded")))
            continue
        st = lstate.setdefault(name, {"last_remaining": None,
                                      "no_progress_streak": 0, "phase": "watch"})
        remaining = loop_remaining(loop["progress_cmd"])
        if remaining is None:
            plog("CHECK B [%s]: remaining unknown — leaving state untouched" % name)
            continue
        alive, pid = loop_alive(os.path.expanduser(loop["lockdir"]))
        last = st.get("last_remaining")
        phase = st.get("phase", "watch")
        made_progress = (last is not None and remaining != last)
        # A no-progress judgment only counts if enough wall time separates two
        # observations. Pulses are ~4h apart; this guards against a manual re-run
        # or a launchd double-fire collapsing "2 consecutive pulses" into seconds
        # and false-flagging a healthy-but-momentarily-static loop as stalled.
        # (A DEAD loop is relaunched regardless — that path ignores this guard.)
        now = time.time()
        last_ts = st.get("last_ts")
        min_s = float(cfg.get("pulse_min_progress_interval_hours", 3)) * 3600
        elapsed_ok = (last_ts is None) or ((now - last_ts) >= min_s)
        st["last_ts"] = now
        plog("CHECK B [%s]: remaining=%d last=%s alive=%s(pid=%s) phase=%s streak=%d elapsed_ok=%s"
             % (name, remaining, last, alive, pid, phase,
                st.get("no_progress_streak", 0), elapsed_ok))

        if remaining == 0:
            # work complete -> healthy-idle, never a stall. reset.
            st.update(last_remaining=0, no_progress_streak=0, phase="watch")
        elif phase == "blocked":
            # already escalated to an ERROR card; no more auto-attempts until a
            # human closes it. Recovery is signalled by progress resuming.
            if made_progress:
                st.update(phase="watch", no_progress_streak=0)
                plog("  [%s] progress resumed while blocked -> back to watch" % name)
            st["last_remaining"] = remaining
        elif phase == "relaunched":
            if made_progress:
                st.update(phase="watch", no_progress_streak=0)
                plog("  [%s] relaunch took (progress resumed) -> watch" % name)
            elif not elapsed_ok:
                # too soon after the relaunch to judge — give it a real pulse gap.
                plog("  [%s] relaunched, holding (too soon to judge escalation)" % name)
            else:
                # relaunch attempted and STILL no progress next pulse -> escalate
                body = (
                    "loop: %s (project %s)\n"
                    "remaining: %d (unchanged since the auto-relaunch a pulse ago)\n"
                    "lockdir: %s  alive=%s pid=%s\n\n"
                    "estate-pulse relaunched this loop via its own resume path and it\n"
                    "still made no progress. Auto-relaunch is now DISABLED for this loop\n"
                    "until this card is closed (no relaunch storms). Investigate:\n"
                    "  tail -n 40 ~/Projects/lantern/data/model-watch/resume-jobs.log\n"
                    "  tail -n 40 ~/Projects/lantern/data/second-judge-backfill.log\n"
                    "  tail -n 40 ~/Projects/lantern/data/axes-backfill.log\n"
                    "Likely a deterministic failure (missing ollama model, dead key/credits,\n"
                    "a poison row). Fix the cause, then close this card to re-arm.\n\n"
                    "key pulse:loop-stalled:%s"
                    % (name, proj, remaining, loop["lockdir"], alive, pid, name)
                )
                file_card(proj, "tracked loop %s stalled (relaunch did not take)" % name,
                          body, "pulse:loop-stalled:%s" % name, "error")
                st.update(phase="blocked", no_progress_streak=0)
            st["last_remaining"] = remaining
        else:  # phase == watch
            if not alive:
                # dead/stopped loop with work outstanding -> the overnight case.
                # Relaunch immediately, even on the first observation (cold state).
                do_relaunch(loop, relaunched_this_run)
                body = (
                    "loop: %s (project %s)\n"
                    "remaining: %d, but the loop was NOT running (lockdir=%s alive=%s pid=%s)\n\n"
                    "estate-pulse relaunched it via the project's own idempotent resume path:\n"
                    "  %s\n\n"
                    "Informational: the loop had stopped (breaker tripped or process dropped)\n"
                    "with work left and nothing had relaunched it. If it does not make progress\n"
                    "by the next pulse an ERROR card follows and auto-relaunch stops.\n\n"
                    "key pulse:loop-relaunch:%s"
                    % (name, proj, remaining, loop["lockdir"], alive, pid,
                       loop["relaunch_cmd"], name)
                )
                file_card(proj, "tracked loop %s was down — relaunched" % name,
                          body, "pulse:loop-relaunch:%s" % name, "warn")
                st.update(phase="relaunched", no_progress_streak=0, last_remaining=remaining)
            elif last is None:
                # first sight of a LIVE loop -> baseline only, no judgment yet.
                st.update(no_progress_streak=0, last_remaining=remaining)
            elif made_progress:
                st.update(no_progress_streak=0, last_remaining=remaining)
            elif not elapsed_ok:
                # too soon since last observation to count a no-progress pulse.
                plog("  [%s] no change but < min interval — not counting a stall pulse" % name)
                st["last_remaining"] = remaining
            else:
                streak = st.get("no_progress_streak", 0) + 1
                st["no_progress_streak"] = streak
                st["last_remaining"] = remaining
                if streak >= 2:
                    # live pid but no progress across 2 consecutive pulses
                    do_relaunch(loop, relaunched_this_run)
                    body = (
                        "loop: %s (project %s)\n"
                        "remaining: %d, unchanged across 2 consecutive pulses despite a live\n"
                        "pid (%s). Loop appears wedged.\n\n"
                        "estate-pulse attempted a relaunch via the resume path:\n  %s\n"
                        "(A live lock is left in place, so a truly-live loop's relaunch is a\n"
                        "safe no-op; a wedged one that does not recover escalates next pulse.)\n\n"
                        "key pulse:loop-relaunch:%s"
                        % (name, proj, remaining, pid, loop["relaunch_cmd"], name)
                    )
                    file_card(proj, "tracked loop %s not progressing — relaunched" % name,
                              body, "pulse:loop-relaunch:%s" % name, "warn")
                    st.update(phase="relaunched", no_progress_streak=0)
    return state


# ---------------------------------------------------------------- CHECK C
def check_stale_prs(cfg):
    repos = cfg.get("pulse_repos", [])
    idle_max_h = cfg.get("pulse_pr_idle_hours", 18)
    gh = find_gh()
    if not gh:
        plog("CHECK C: gh not found — skipping")
        return
    now = time.time()
    stale = []
    for slug in repos:
        try:
            r = subprocess.run(
                [gh, "pr", "list", "--repo", slug, "--state", "open",
                 "--json", "number,title,updatedAt,url", "--limit", "50"],
                capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                plog("CHECK C: gh pr list %s failed: %s" % (slug, r.stderr.strip()[:120]))
                continue
            for pr in json.loads(r.stdout or "[]"):
                ts = pr.get("updatedAt", "")
                try:
                    upd = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                idle_h = (now - upd) / 3600
                if idle_h > idle_max_h:
                    stale.append((slug, pr.get("number"), pr.get("title", ""),
                                  idle_h, pr.get("url", "")))
        except Exception as e:
            plog("CHECK C: %s error: %r" % (slug, e))
    plog("CHECK C: %d PR(s) idle > %dh" % (len(stale), idle_max_h))
    if not stale:
        return
    stale.sort(key=lambda x: -x[3])
    lines = ["- %s#%s: %s (idle %.0fh)\n  %s" % (s, n, t, h, u)
             for (s, n, t, h, u) in stale]
    body = (
        "Open PRs with no update in over %dh:\n\n%s\n\n"
        "Merge, push, or close them. This one card rolls up all stale PRs and stays\n"
        "deduped (key pulse:stale-prs) while the set is open; close it once cleared."
        % (idle_max_h, "\n".join(lines))
    )
    file_card("ops-routines", "estate-pulse: %d stale open PR(s)" % len(stale),
              body, "pulse:stale-prs", "warn")


def curl_head_ok(url, timeout=5):
    try:
        r = subprocess.run(["/usr/bin/curl", "-sS", "-o", "/dev/null", "-I",
                            "-m", str(timeout), url],
                           capture_output=True, text=True, timeout=timeout + 3)
        return r.returncode == 0
    except Exception:
        return False


def connectivity_ok(cfg):
    # online = ANY probe reachable (conservative against a single flaky host false-flagging outage)
    for u in cfg.get("connectivity_probe_urls", ["https://1.1.1.1/", "https://api.github.com"]):
        if curl_head_ok(u):
            return True
    return False


def append_jsonl(rec):
    try:
        with open(FAILURES_JSONL, "a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception as e:
        plog("append_jsonl failed: %r" % e)


def launchd_state():
    try:
        out = subprocess.run(["/bin/launchctl", "list"], capture_output=True,
                             text=True, timeout=30).stdout
    except Exception as e:
        plog("launchd_state: launchctl list failed: %r" % e)
        return {}
    d = {}
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 3:
            d[p[2]] = (p[0], p[1])   # label -> (pid, status)
    return d


def push_heartbeat_gist(cfg, ts):
    gid = cfg.get("heartbeat_gist_id")
    gh = find_gh()
    if not gid or not gh:
        return
    fname = cfg.get("heartbeat_gist_filename", "heartbeat.txt")
    if DRYRUN:
        plog("WOULD push heartbeat gist %s <- %s" % (gid, ts)); return
    try:
        body = json.dumps({"files": {fname: {"content": ts + "\n"}}})
        import tempfile
        tf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        tf.write(body); tf.close()
        r = subprocess.run([gh, "api", "-X", "PATCH", "/gists/" + gid, "--input", tf.name],
                           capture_output=True, text=True, timeout=30)
        os.unlink(tf.name)
        if r.returncode == 0:
            plog("heartbeat gist pushed (%s)" % ts)
        else:
            plog("heartbeat gist push FAILED rc=%d: %s" % (r.returncode, r.stderr.strip()[:160]))
    except Exception as e:
        plog("heartbeat gist push FAILED: %r" % e)


def _dur_str(a_iso, b_iso):
    try:
        a = datetime.fromisoformat(a_iso); b = datetime.fromisoformat(b_iso)
        mins = (b - a).total_seconds() / 60.0
        return "%.0fm" % mins if mins < 90 else "%.1fh" % (mins / 60.0)
    except Exception:
        return "unknown"


def net_state(state):
    return state.setdefault("network", {"outage": False, "outage_start": None, "affected_jobs": []})


def do_recovery_burst(cfg, state):
    ls = launchd_state()
    kicked = []
    for label in cfg.get("recovery_kickstart", []):
        st = ls.get(label)
        if not st:
            continue
        pid, status = st
        # restart only what is actually broken: not-running AND last exit nonzero.
        # (a healthy/auto-recovered tunnel or an exit-0 daily is left alone.)
        if pid == "-" and status != "0":
            if DRYRUN:
                plog("  WOULD recovery-kickstart %s (down, exit %s)" % (label, status))
            else:
                subprocess.run(["/bin/launchctl", "kickstart", "-k",
                                "gui/%d/%s" % (UID, label)],
                               capture_output=True, timeout=30, check=False)
                plog("  recovery-kickstart %s (was exit %s)" % (label, status))
            kicked.append(label)
    relaunched = set()
    for loop in cfg.get("pulse_loops", []):
        rem = loop_remaining(loop["progress_cmd"])
        if rem and rem > 0:
            alive, _ = loop_alive(os.path.expanduser(loop["lockdir"]))
            if not alive:
                do_relaunch(loop, relaunched)
                kicked.append("loop:" + loop["name"])
    return kicked


def handle_network(cfg, state):
    """Decide online/offline; record ONE outage-start event; run recovery burst on restore.
    Returns True if online."""
    ns = net_state(state)
    online = connectivity_ok(cfg)
    now = now_iso()
    if not online:
        if not ns.get("outage"):
            ls = launchd_state()
            affected = [l for l in cfg.get("recovery_kickstart", [])
                        if l in ls and ls[l][0] == "-" and ls[l][1] != "0"]
            ns.update(outage=True, outage_start=now, affected_jobs=affected)
            if not DRYRUN:
                append_jsonl({"ts": now, "type": "outage-start", "project": "ops-routines",
                              "affected_jobs": affected, "host": socket.gethostname()})
            plog("OFFLINE: outage-start recorded (affected=%d). Skipping relaunch + board this run."
                 % len(affected))
        else:
            plog("OFFLINE: still in outage since %s. One event already recorded; skipping."
                 % ns.get("outage_start"))
        return False
    if ns.get("outage"):
        start = ns.get("outage_start"); dur = _dur_str(start, now)
        affected = ns.get("affected_jobs") or []
        kicked = do_recovery_burst(cfg, state)
        body = ("Network recovered at %s after an outage that began %s (~%s dark).\n\n"
                "Network-dependent jobs down at outage start:\n  %s\n\n"
                "Recovery burst kickstarted (only jobs still broken):\n  %s\n\n"
                "One summary card (key pulse:outage) replaces the per-job storm the old\n"
                "pulse would have filed. Close it once you've confirmed the fleet is healthy."
                % (now, start, dur, ", ".join(affected) or "none", ", ".join(kicked) or "none"))
        file_card("ops-routines", "estate outage recovered (~%s dark)" % dur,
                  body, "pulse:outage", "warn")
        ns.update(outage=False, outage_start=None, affected_jobs=[])
        plog("ONLINE: recovery burst done (kicked=%d)" % len(kicked))
    return True


# ---------------------------------------------------------------- CHECK D
def _proc_identity(pid):
    """Process start time, or None if the pid is gone. Start time is the identity check:
    a bare pid can be recycled by an unrelated process, and 'my job is still running'
    must not be satisfied by a stranger wearing its number."""
    try:
        p = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart="],
                           capture_output=True, text=True, timeout=20)
        return p.stdout.strip() or None
    except Exception:
        return None


def check_session_jobs(cfg):
    """Watch one-off long-running jobs Jeff launches by hand (config: session_jobs).

    These are not launchd jobs and not tracked loops — they are detached overnight runs
    with a stated hard stop. This runs in BOTH heartbeat and full mode, because the
    hourly heartbeat is the only tick that happens overnight. Terminal outcomes are
    reported once and the entry removes itself, so the config does not collect ghosts.
    """
    jobs = cfg.get("session_jobs") or []
    if not jobs:
        return
    now = datetime.now(timezone.utc).astimezone()
    remaining, resolved = [], []
    for j in jobs:
        name, pid = j.get("name", "?"), j.get("pid")
        try:
            hard_stop = datetime.fromisoformat(j["hard_stop"])
        except Exception:
            plog("CHECK D: %s has an unreadable hard_stop — skipping" % name)
            remaining.append(j)
            continue
        ident = _proc_identity(pid)
        alive = bool(ident) and (not j.get("identity") or ident == j.get("identity"))
        log_path = os.path.expanduser(j.get("log", "") or "")
        tail_txt = tail(log_path, 20) if log_path and os.path.isfile(log_path) else "(no log)"
        mins_left = (hard_stop - now).total_seconds() / 60.0

        if alive and now < hard_stop:
            plog("CHECK D: %s alive (pid %s, %.0f min to hard stop)" % (name, pid, mins_left))
            remaining.append(j)
            continue

        if alive:   # past its own hard stop
            over = -mins_left
            plog("CHECK D: %s OVERRAN hard stop by %.0f min" % (name, over))
            file_card(j.get("project", "infra"),
                      "session job %s overran its hard stop by %.0f min" % (name, over),
                      "pid %s is still running past the stated hard stop (%s).\n\n"
                      "NOT killed — stopping someone's overnight run is their call. To stop it:\n"
                      "  kill %s\n\nLog tail (%s):\n%s"
                      % (pid, j.get("hard_stop"), pid, log_path or "none", tail_txt),
                      "pulse:session-job:%s:overran" % name, "warn")
            remaining.append(j)   # keep watching; it may still finish
            continue

        # Gone. Prefer the truth over the timing heuristic: runlog writes an
        # "# runlog[tag] EXIT=<code>" line into the log, which survives even when the
        # caller sent stdout and stderr to /dev/null. Guessing from timing alone called a
        # successful 41-minute run an abort on 2026-07-24 — a job can simply finish early.
        exit_code = None
        for line in reversed(tail_txt.splitlines()):
            m = re.search(r"EXIT=(-?\d+)", line)
            if m:
                exit_code = int(m.group(1))
                break
        if exit_code is not None:
            sev = "warn" if exit_code == 0 else "error"
            headline = "finished (exit 0)" if exit_code == 0 else "FAILED (exit %d)" % exit_code
            how = ("finished with EXIT=%d" % exit_code if exit_code == 0
                   else "FAILED with EXIT=%d" % exit_code)
        else:
            early = (hard_stop - now).total_seconds() > 3600
            sev = "error" if early else "warn"
            headline = "ended early, outcome unknown" if early else "finished"
            how = ("ended more than an hour before its %s hard stop, and its log carries no "
                   "runlog EXIT marker — so this is either an early success or an abort, and "
                   "the log tail below is the only way to tell" % j.get("hard_stop")) if early \
                else "ended at/near its hard stop (no EXIT marker in the log)"
        plog("CHECK D: %s gone (%s) — filing %s" % (name, how, sev))
        file_card(j.get("project", "infra"),
                  "session job %s %s" % (name, headline),
                  "%s\n\nStarted: %s\nHard stop: %s\nEnded: seen gone at %s\n\n"
                  "%s\n\nLog tail (%s):\n%s"
                  % (how, j.get("started", "?"), j.get("hard_stop"), now.isoformat(timespec="seconds"),
                     j.get("note", ""), log_path or "none", tail_txt),
                  "pulse:session-job:%s:ended" % name, sev)
        resolved.append(name)

    if resolved and not DRYRUN:
        cfg["session_jobs"] = remaining
        write_atomic(CONFIG, cfg)
        plog("CHECK D: removed resolved job(s): %s" % ", ".join(resolved))


def write_heartbeat(cfg, state, online, mode):
    ns = net_state(state)
    now = now_iso()
    prev = load_json(HEARTBEAT, {})
    hb = {"last_run": now, "outage": bool(ns.get("outage")),
          "host": socket.gethostname(), "mode": mode,
          "last_ok": now if online else prev.get("last_ok")}
    if not DRYRUN:
        write_atomic(HEARTBEAT, hb)
        if online:
            push_heartbeat_gist(cfg, now)   # push only succeeds when online anyway
    return hb


def main():
    os.makedirs(FAILDIR, exist_ok=True)
    cfg = load_json(CONFIG, {})
    state = load_json(STATE, {})
    mode = "heartbeat" if HEARTBEAT_MODE else "full"
    plog("run start (mode=%s dry-run=%s)" % (mode, DRYRUN))
    online = handle_network(cfg, state)      # outage-start record / recovery burst
    write_heartbeat(cfg, state, online, mode)
    check_session_jobs(cfg)   # both modes: the hourly heartbeat is the only overnight tick
    if mode == "full":
        if online:
            check_launchd_exits(cfg)
            state = check_loops(cfg, state)
            check_stale_prs(cfg)
        else:
            # the 08:00 failure mode: do NOT relaunch a loop into a dead network, and
            # do NOT file 6 per-job cards — handle_network already logged one outage event.
            plog("full pulse: OFFLINE — skipped CHECK A/B/C (outage mode).")
    if not DRYRUN:
        state["_updated"] = now_iso()
        write_atomic(STATE, state)
    plog("run done (mode=%s online=%s)" % (mode, online))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # a pulse must never break fleet-sentinel's run
        sys.stderr.write("estate-pulse: INTERNAL ERROR (exiting 0): %r\n" % e)
    sys.exit(0)
