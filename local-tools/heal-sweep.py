#!/usr/bin/env python3
"""heal-sweep — close FAILURE board cards whose condition has already healed.

Board card T-1735 (issue #739, approved by Jeff 2026-08-31): a FAILURE card that
stays open after the underlying job self-heals is habit-debt (creak ledger entry
5) — 30 were closed by hand on 2026-08-29. This is the machine version, run
4x/day inside fleet-sentinel alongside estate-pulse.

Reads the READ side (never pages the live board for a sweep, per memory
"Board close ritual"): the nightly export at
~/data-vaults/gh-board/<latest-date>/items.json. Every open item whose body
carries a `failkey: <key>` line (the failtask stamp) is classified into exactly
one machine-checkable heal class, or "no machine-checkable heal" if none apply.
Only real evidence closes a card — never a guess.

Heal classes:
  1. launchd:<label> / pulse:launchd:<label>
     -> `launchctl list` shows the job currently running (pid != '-'), OR
        last-exit == 0 AND the job's StandardOut/ErrorPath log (from its plist)
        has an mtime newer than the card's `filed:` timestamp.
  2. <project>-snapshot-failed / rally-d1-export-failed  (the quartet backup
     lanes: family-archive, notes-vault, music-atlas, photo-atlas, lantern-data,
     rally-d1)
     -> that lane's last-ok.json exists and its last_ok timestamp is newer than
        the card's `filed:` timestamp.
  3. memory-sync-conflict-*
     -> `memory-sync --check` exits 0 right now.

Everything else is left open and counted as "no machine-checkable heal" by
failkey-class prefix, never guessed at.

A healed card gets: a dated `healed: <ts> <evidence>` comment on the real Issue
(gh issue comment — cards are real issues in bigbrownjeff/board, not
DraftIssues, so this is the addComment equivalent), Status -> Done via
`gh project item-edit`, and the issue closed via `gh issue close`. Reopen-on-
recurrence is unchanged: failtask reopens a Done card whose failkey fires
again, exactly as today.

Usage:
  heal-sweep.py                 # real run
  heal-sweep.py --dry-run       # print the would-close list, write nothing
  heal-sweep.py --quiet         # one-line summary only (what fleet-sentinel runs)
"""
import glob
import json
import os
import plistlib
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
GH_BOARD_EXPORTS = os.path.join(HOME, "data-vaults", "gh-board")
FAILDIR = os.path.join(HOME, ".claude", "failures")
PULSE_LOG = os.path.join(FAILDIR, "pulse.log")
MEMORY_SYNC = os.path.join(HOME, ".claude", "bin", "memory-sync")

PROJECT_ID = "PVT_kwHOARM7z84Bd1kf"
FIELD_STATUS = "PVTSSF_lAHOARM7z84Bd1kfzhYULaw"
STATUS_DONE_OPT = "98236657"
BOARD_REPO = "bigbrownjeff/board"
PACE_S = 1.6

DRYRUN = "--dry-run" in sys.argv[1:]
QUIET = "--quiet" in sys.argv[1:]

FAILKEY_RE = re.compile(r"failkey:\s*(\S+)")
FILED_RE = re.compile(r"filed:\s*(\S+)")
LAUNCHD_RE = re.compile(r"^(?:pulse:)?launchd:(.+)$")

# Quartet backup lanes with a tested last-ok.json contract (see
# CLAUDE.md "Backup coverage" anchors + each ops/data_snapshot.sh).
BACKUP_LANES = {
    "family-archive-snapshot-failed": "~/data-vaults/family-archive/last-ok.json",
    "notes-vault-snapshot-failed": "~/data-vaults/notes-vault/last-ok.json",
    "music-atlas-snapshot-failed": "~/data-vaults/music-atlas/last-ok.json",
    "photo-atlas-snapshot-failed": "~/data-vaults/photo-atlas/last-ok.json",
    "lantern-data-snapshot-failed": "~/lantern-data-vault/last-ok.json",
    "rally-d1-export-failed": "~/data-vaults/rally/last-ok.json",
}


def log(msg):
    line = "%s %s%s" % (now_iso(), "[dry-run] " if DRYRUN else "", msg)
    try:
        with open(PULSE_LOG, "a") as f:
            f.write("heal-sweep: " + line + "\n")
    except Exception:
        pass
    if not QUIET:
        print("heal-sweep: " + msg)


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def find_gh():
    for c in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh"):
        if os.access(c, os.X_OK):
            return c
    from shutil import which
    return which("gh")


def latest_export():
    dirs = sorted(glob.glob(os.path.join(GH_BOARD_EXPORTS, "20*-*-*")))
    for d in reversed(dirs):
        p = os.path.join(d, "items.json")
        if os.path.isfile(p):
            return p
    return None


def parse_ts(s):
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def load_open_failure_cards():
    path = latest_export()
    if not path:
        log("no gh-board nightly export found under %s — nothing to sweep" % GH_BOARD_EXPORTS)
        return []
    with open(path) as f:
        items = json.load(f)["items"]
    log("reading %s (%d items)" % (path, len(items)))
    out = []
    for it in items:
        if it.get("status") == "Done":
            continue
        content = it.get("content") or {}
        if content.get("type") != "Issue":
            continue  # only real Issues are closeable this way
        body = content.get("body") or ""
        m = FAILKEY_RE.search(body)
        if not m:
            continue
        fm = FILED_RE.search(body)
        filed_ts = parse_ts(fm.group(1)) if fm else None
        out.append({
            "item_id": it.get("id"),
            "ref": it.get("ref"),
            "title": content.get("title"),
            "number": content.get("number"),
            "repository": (content.get("repository") or "").replace("https://github.com/", ""),
            "failkey": m.group(1),
            "filed_ts": filed_ts,
        })
    return out


# --------------------------------------------------------------- launchd class
_launchctl_cache = None


def launchctl_status(label):
    global _launchctl_cache
    if _launchctl_cache is None:
        _launchctl_cache = {}
        try:
            out = subprocess.run(["/bin/launchctl", "list"], capture_output=True,
                                 text=True, timeout=30).stdout
        except Exception as e:
            log("launchctl list failed: %r" % e)
            out = ""
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                _launchctl_cache[parts[2]] = (parts[0], parts[1])
    return _launchctl_cache.get(label)


def plist_log_path(label):
    plist = os.path.join(HOME, "Library", "LaunchAgents", label + ".plist")
    try:
        with open(plist, "rb") as f:
            d = plistlib.load(f)
        return d.get("StandardErrorPath") or d.get("StandardOutPath") or None
    except Exception:
        return None


def check_launchd(label, filed_ts):
    st = launchctl_status(label)
    if st is None:
        return None, "job %s not currently loaded — no live signal to check" % label
    pid, code = st
    if pid != "-":
        return "job %s currently running (pid %s)" % (label, pid), None
    if code != "0":
        return None, "job %s last-exit still %s" % (label, code)
    logpath = plist_log_path(label)
    if not logpath or not os.path.isfile(logpath):
        return None, "job %s exit 0 but no log to confirm it ran after filing" % label
    mtime = os.path.getmtime(logpath)
    if filed_ts is None:
        return None, "job %s exit 0 but card has no parseable filed timestamp" % label
    if mtime > filed_ts:
        return ("job %s: launchctl last-exit 0; log %s modified %s (after filed %s)"
                % (label, logpath, datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
                   datetime.fromtimestamp(filed_ts).isoformat(timespec="seconds")), None)
    return None, "job %s exit 0 but log %s predates the card (filed after last run)" % (label, logpath)


# --------------------------------------------------------------- backup class
# Lanes whose last-ok.json is written BEFORE their offsite step (so a newer
# last_ok does not by itself prove offsite succeeded — verified 2026-08-31 on
# lantern's data_snapshot.sh, where last-ok.json line precedes the offsite
# verify/die block) fall back to the SAME evidence the paired launchd job's
# CHECK A class already trusts: launchd exit 0 + the job's own log newer than
# the card, which covers the whole script including offsite. Lanes whose
# last-ok.json carries its own "offsite" field (family-archive, notes-vault,
# music-atlas, photo-atlas, rally) prove it inline and need no fallback.
BACKUP_LAUNCHD_FALLBACK = {
    "lantern-data-snapshot-failed": "com.jeffpinto.lantern-data-snapshot",
}


def check_backup(failkey, filed_ts):
    rel = BACKUP_LANES.get(failkey)
    if not rel:
        return None, None  # not this class
    path = os.path.expanduser(rel)
    if not os.path.isfile(path):
        return None, "no %s heal — %s does not exist" % (failkey, path)
    try:
        with open(path) as f:
            d = json.load(f)
        last_ok = parse_ts(d.get("last_ok", ""))
    except Exception as e:
        return None, "no %s heal — %s unreadable (%r)" % (failkey, path, e)
    if last_ok is None:
        return None, "no %s heal — %s has no parseable last_ok" % (failkey, path)
    if filed_ts is not None and last_ok <= filed_ts:
        return None, "no %s heal — last_ok %s predates the card (filed %s)" % (
            failkey, d.get("last_ok"), datetime.fromtimestamp(filed_ts).isoformat(timespec="seconds"))
    if "offsite" in d:
        if d.get("offsite") is not True:
            return None, "no %s heal — last-ok.json offsite=%r, not true" % (failkey, d.get("offsite"))
        return "%s: last-ok.json %s newer than card, offsite=true (last_ok=%s)" % (
            failkey, path, d.get("last_ok")), None
    fallback_label = BACKUP_LAUNCHD_FALLBACK.get(failkey)
    if not fallback_label:
        return None, ("no %s heal — last-ok.json has no offsite field and no launchd "
                       "fallback is configured for this lane" % failkey)
    evidence, reason = check_launchd(fallback_label, filed_ts)
    if evidence:
        return "%s: last-ok.json %s newer than card, AND %s (covers offsite)" % (
            failkey, path, evidence), None
    return None, "%s: last-ok.json newer than card but launchd fallback unproven — %s" % (failkey, reason)


# --------------------------------------------------------------- memory-sync class
_memsync_checked = None


def check_memory_sync():
    global _memsync_checked
    if _memsync_checked is not None:
        return _memsync_checked
    if not os.access(MEMORY_SYNC, os.X_OK):
        _memsync_checked = (None, "memory-sync binary not found/executable")
        return _memsync_checked
    try:
        r = subprocess.run([MEMORY_SYNC, "--check"], capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            _memsync_checked = ("memory-sync --check exit 0 at %s" % now_iso(), None)
        else:
            _memsync_checked = (None, "memory-sync --check exit %d — drift still present" % r.returncode)
    except Exception as e:
        _memsync_checked = (None, "memory-sync --check failed to run: %r" % e)
    return _memsync_checked


# --------------------------------------------------------------------- classify
def classify(card):
    fk = card["failkey"]
    m = LAUNCHD_RE.match(fk)
    if m:
        return check_launchd(m.group(1), card["filed_ts"])
    if fk in BACKUP_LANES:
        return check_backup(fk, card["filed_ts"])
    if fk.startswith("memory-sync-conflict-"):
        return check_memory_sync()
    return None, "no machine-checkable heal for failkey class"


def class_of(failkey):
    if LAUNCHD_RE.match(failkey):
        return "launchd"
    if failkey in BACKUP_LANES:
        return "backup"
    if failkey.startswith("memory-sync-conflict-"):
        return "memory-sync"
    # a stable-ish bucket name for reporting only
    return failkey.split(":", 1)[0] if ":" in failkey else re.sub(r"[0-9a-f]{8,}$", "", failkey).rstrip("-") or failkey


# ----------------------------------------------------------------------- close
def already_closed(gh_bin, repo, number):
    """Live re-check before acting: the READ side (nightly export) can be up to
    ~20h stale against 4x/day runs, so a card this same day's earlier run
    already closed would otherwise get a duplicate healed-comment + a no-op
    close on every later pass until tomorrow's export refreshes. One cheap
    REST read avoids that."""
    try:
        r = subprocess.run([gh_bin, "api", "repos/%s/issues/%s" % (repo, number),
                            "--jq", ".state"], capture_output=True, text=True, timeout=60)
        return r.returncode == 0 and r.stdout.strip().upper() == "CLOSED"
    except Exception:
        return False  # unknown -> proceed, worst case a harmless duplicate comment


def close_card(gh_bin, card, evidence):
    stamp = "healed: %s %s" % (now_iso(), evidence)
    repo = card["repository"] or BOARD_REPO
    number = card["number"]
    if DRYRUN:
        log("DRY-RUN would comment+close %s#%s (%s): %s" % (repo, number, card["ref"], stamp))
        return True
    if already_closed(gh_bin, repo, number):
        log("SKIP %s#%s (T-%s) — already closed since the export was read, no duplicate action"
            % (repo, number, card["ref"]))
        return True
    ok = True
    try:
        r = subprocess.run([gh_bin, "issue", "comment", str(number), "-R", repo, "--body", stamp],
                           capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            log("comment failed for %s#%s: %s" % (repo, number, r.stderr.strip()[:200]))
            ok = False
    except Exception as e:
        log("comment error for %s#%s: %r" % (repo, number, e))
        ok = False
    time.sleep(PACE_S)
    try:
        r = subprocess.run([gh_bin, "project", "item-edit", "--project-id", PROJECT_ID,
                            "--id", card["item_id"], "--field-id", FIELD_STATUS,
                            "--single-select-option-id", STATUS_DONE_OPT],
                           capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            log("status edit failed for %s#%s: %s" % (repo, number, r.stderr.strip()[:200]))
            ok = False
    except Exception as e:
        log("status edit error for %s#%s: %r" % (repo, number, e))
        ok = False
    time.sleep(PACE_S)
    try:
        r = subprocess.run([gh_bin, "issue", "close", str(number), "-R", repo,
                            "--reason", "completed"],
                           capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            log("close failed for %s#%s: %s" % (repo, number, r.stderr.strip()[:200]))
            ok = False
    except Exception as e:
        log("close error for %s#%s: %r" % (repo, number, e))
        ok = False
    time.sleep(PACE_S)
    return ok


def main():
    gh_bin = find_gh()
    if not gh_bin:
        log("gh not found — nothing to do")
        return 0
    cards = load_open_failure_cards()
    closed, skipped = [], {}
    for card in cards:
        evidence, reason = classify(card)
        if evidence:
            closed.append((card, evidence))
        else:
            skipped.setdefault(class_of(card["failkey"]), []).append((card, reason))

    n_closed = 0
    for card, evidence in closed:
        if close_card(gh_bin, card, evidence):
            n_closed += 1
            log("HEALED T-%s [%s] %s -> %s" % (card["ref"], card["failkey"], card["title"], evidence))
        else:
            log("HEAL FAILED T-%s [%s] %s (mutation error, left open)" % (card["ref"], card["failkey"], card["title"]))

    skip_count = sum(len(v) for v in skipped.values())
    report = ("standing report: heal-sweep closed %d/%d candidate(s); %d card(s) had no "
              "machine-checkable heal (%s)"
              % (n_closed, len(closed), skip_count,
                 ", ".join("%s=%d" % (k, len(v)) for k, v in sorted(skipped.items(), key=lambda x: -len(x[1]))[:8])))
    # Always loud, even under --quiet: this is the one line that proves a heal
    # ran and what it did, per memory fixes-that-quiet-alerts-leave-a-trace.
    try:
        with open(PULSE_LOG, "a") as f:
            f.write("heal-sweep: %s %s\n" % (now_iso(), report))
    except Exception:
        pass
    print("heal-sweep: " + report)
    if not QUIET:
        for card, evidence in closed[:20]:
            print("  CLOSE  T-%s [%s] %s" % (card["ref"], card["failkey"], card["title"]))
            print("         evidence: %s" % evidence)
    return 0


if __name__ == "__main__":
    sys.exit(main())
