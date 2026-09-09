#!/usr/bin/env python3
"""morning-digest — one vault note each morning answering "what changed
overnight, in one place." L4 build from the 2026-08-31 ecosystem-unification
plan (~/Projects/_hygiene/ecosystem-unification-2026-08-31.md, section 3).

Gathers, over the trailing 24h:
  1. board deltas   — diff the two newest ~/data-vaults/gh-board/*/items.json
                       snapshots (new cards, closed cards, status changes)
  2. CRM deltas     — read-only from outbound_with_jeff_and_marv/data/crm.db
                       (new interactions, drafts staged, next_step/task changes)
  3. git activity   — repos under ~/Projects/* with commits in the window
  4. launchd health — count of loaded com.jeff* jobs with a nonzero last exit
  5. vault notes    — notes added to the Notes Vault itself

Renders a compact markdown digest and logs it to the vault under a stable
daily id (digest-YYYY-MM-DD) via the blessed `python3 -m notes_vault log`
write path, so a same-day re-run replaces the same note (idempotent; the
vault's content-hash check leaves it alone if nothing changed).

Style: counts first, names capped at 5 per section, no em or en dashes,
pure stdlib. No LLM calls (cost discipline, same rule as estate-pulse.py).
"""
import glob
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

HOME = os.path.expanduser("~")
BOARD_DIR = os.path.join(HOME, "data-vaults", "gh-board")
CRM_DB = os.path.join(HOME, "Projects", "outbound_with_jeff_and_marv", "data", "crm.db")
VAULT_DB = os.path.join(HOME, "Projects", "notes-vault", "data", "notes.db")
VAULT_REPO = os.path.join(HOME, "Projects", "notes-vault")
PROJECTS_DIR = os.path.join(HOME, "Projects")
CAP = 5

DRY_RUN = "--dry-run" in sys.argv[1:]


def now_utc():
    return datetime.now(timezone.utc)


def since_iso(hours=24):
    return (now_utc() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- 1. board --

def _load_snapshot(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return {it["id"]: it for it in data.get("items", [])}


def board_deltas():
    dirs = sorted(d for d in glob.glob(os.path.join(BOARD_DIR, "*"))
                  if os.path.isfile(os.path.join(d, "items.json")))
    if len(dirs) < 2:
        return {"ok": False, "reason": "fewer than 2 board snapshots found",
                "new": [], "closed": [], "status_changed": []}
    old_items = _load_snapshot(os.path.join(dirs[-2], "items.json"))
    new_items = _load_snapshot(os.path.join(dirs[-1], "items.json"))

    def label(it):
        ref = it.get("ref")
        tref = ("T-" + str(ref)) if ref else it.get("id", "?")
        return tref + " " + str(it.get("title", "")).strip()

    new_cards, closed_cards, status_changed = [], [], []
    for iid, it in new_items.items():
        old = old_items.get(iid)
        if old is None:
            new_cards.append(label(it))
            continue
        old_status, new_status = old.get("status"), it.get("status")
        if old_status != new_status:
            if new_status == "Done" and old_status != "Done":
                closed_cards.append(label(it))
            else:
                status_changed.append(label(it) + " (" + str(old_status)
                                       + " -> " + str(new_status) + ")")
    return {"ok": True, "new": new_cards, "closed": closed_cards,
            "status_changed": status_changed,
            "old_snapshot": os.path.basename(dirs[-2]),
            "new_snapshot": os.path.basename(dirs[-1])}


# ------------------------------------------------------------------ 2. crm --

def ro_connect(path):
    """Open a WAL database for reading WITHOUT the URI mode=ro trap.

    A `file:...?mode=ro` handle cannot create the -shm file a WAL reader
    needs, so it raises "unable to open database file" on exactly the runs
    that follow a clean writer close (memory sqlite-mode-ro-wal-side-files;
    outbound PR #486 shipped this same fix). 2026-09-08 07:05 the digest
    died that way on notes.db and produced no digest at all. A normal open
    with query_only refuses writes and is allowed to create the side files.
    """
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA query_only=1")
    return conn

def crm_deltas():
    if not os.path.isfile(CRM_DB):
        return {"ok": False, "reason": "crm.db not found"}
    since = since_iso()
    conn = ro_connect(CRM_DB)
    conn.row_factory = sqlite3.Row
    try:
        interactions = conn.execute(
            "SELECT i.id, i.type, i.occurred_at, c.name AS contact_name "
            "FROM interactions i LEFT JOIN contacts c ON c.id = i.contact_id "
            "WHERE i.created_at >= ? ORDER BY i.created_at DESC", (since,)).fetchall()
        drafts = conn.execute(
            "SELECT d.id, d.channel, d.created_at, c.name AS contact_name "
            "FROM drafts d LEFT JOIN contacts c ON c.id = d.contact_id "
            "WHERE d.created_at >= ? ORDER BY d.created_at DESC", (since,)).fetchall()
        task_changes = conn.execute(
            "SELECT id, key, title, due_state, updated_at FROM tasks "
            "WHERE updated_at >= ? ORDER BY updated_at DESC", (since,)).fetchall()
    finally:
        conn.close()
    return {
        "ok": True,
        "interactions_count": len(interactions),
        "interactions_top": [(r["contact_name"] or "?") + " (" + (r["type"] or "?") + ")"
                              for r in interactions[:CAP]],
        "drafts_count": len(drafts),
        "drafts_top": [(r["contact_name"] or "?") + " (" + (r["channel"] or "?") + ")"
                       for r in drafts[:CAP]],
        "task_changes_count": len(task_changes),
        "task_changes_top": [r["title"] for r in task_changes[:CAP]],
    }


# ------------------------------------------------------------- 3. git repos --

def git_activity():
    since = "24 hours ago"
    repos = []
    for entry in sorted(os.listdir(PROJECTS_DIR)):
        repo = os.path.join(PROJECTS_DIR, entry)
        if not os.path.isdir(os.path.join(repo, ".git")):
            continue
        try:
            count_out = subprocess.run(
                ["git", "-C", repo, "rev-list", "--count", "--all",
                 "--since=" + since], capture_output=True, text=True, timeout=10)
            count = int((count_out.stdout or "0").strip() or "0")
        except Exception:
            continue
        if count <= 0:
            continue
        try:
            subj_out = subprocess.run(
                ["git", "-C", repo, "log", "--all", "--since=" + since,
                 "--pretty=%s", "-1"], capture_output=True, text=True, timeout=10)
            subject = (subj_out.stdout or "").strip()
        except Exception:
            subject = ""
        repos.append({"repo": entry, "count": count, "latest_subject": subject})
    repos.sort(key=lambda r: r["count"], reverse=True)
    return repos


# --------------------------------------------------------- 4. launchd health --

def launchd_health():
    try:
        out = subprocess.run(["/bin/launchctl", "list"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception as exc:
        return {"ok": False, "reason": repr(exc), "failures": 0}
    failures = 0
    for line in out.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pid, code, label = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not (label.startswith("com.jeffpinto.") or label.startswith("com.jeff.")):
            continue
        if pid == "-" and code not in ("0", "-"):
            failures += 1
    return {"ok": True, "failures": failures}


# ------------------------------------------------------------ 5. vault notes --

def vault_notes_added():
    if not os.path.isfile(VAULT_DB):
        return {"ok": False, "reason": "notes.db not found"}
    since = since_iso()
    conn = ro_connect(VAULT_DB)
    conn.row_factory = sqlite3.Row
    try:
        # created_at (from the note's own frontmatter), not imported_at: a
        # wiki rebuild re-imports unchanged notes and would otherwise make
        # every rebuild look like new content.
        rows = conn.execute(
            "SELECT title, source_app FROM notes WHERE created_at >= ? "
            "AND source_id NOT LIKE 'digest-%' ORDER BY created_at DESC",
            (since,)).fetchall()
    finally:
        conn.close()
    return {"ok": True, "count": len(rows),
            "top": [r["title"] + " (" + r["source_app"] + ")" for r in rows[:CAP]]}


# ---------------------------------------------------------------- render ----

def _bullet_list(items, cap=CAP):
    shown = items[:cap]
    lines = ["- " + s for s in shown]
    remainder = len(items) - len(shown)
    if remainder > 0:
        lines.append("- and " + str(remainder) + " more")
    return lines


def render(day, board, crm, git_repos, launchd, vault):
    lines = []
    lines.append("# Morning digest, " + day)
    lines.append("")
    lines.append("What changed in the last 24 hours, in one place.")
    lines.append("")

    lines.append("## Board")
    if not board["ok"]:
        lines.append(board["reason"] + ".")
    else:
        lines.append("New: " + str(len(board["new"])) + ". Closed: "
                      + str(len(board["closed"])) + ". Status changed: "
                      + str(len(board["status_changed"])) + ".")
        if board["new"]:
            lines.append("New cards:")
            lines.extend(_bullet_list(board["new"]))
        if board["closed"]:
            lines.append("Closed cards:")
            lines.extend(_bullet_list(board["closed"]))
        if board["status_changed"]:
            lines.append("Status changes:")
            lines.extend(_bullet_list(board["status_changed"]))
    lines.append("")

    lines.append("## CRM (outbound_with_jeff_and_marv)")
    if not crm["ok"]:
        lines.append(crm["reason"] + ".")
    else:
        lines.append("New interactions: " + str(crm["interactions_count"])
                      + ". Drafts staged: " + str(crm["drafts_count"])
                      + ". Task changes: " + str(crm["task_changes_count"]) + ".")
        if crm["interactions_top"]:
            lines.append("Interactions:")
            lines.extend(_bullet_list(crm["interactions_top"]))
        if crm["drafts_top"]:
            lines.append("Drafts:")
            lines.extend(_bullet_list(crm["drafts_top"]))
        if crm["task_changes_top"]:
            lines.append("Task changes:")
            lines.extend(_bullet_list(crm["task_changes_top"]))
    lines.append("")

    lines.append("## Git activity")
    if not git_repos:
        lines.append("No commits in the last 24 hours.")
    else:
        lines.append(str(len(git_repos)) + " repos with commits.")
        shown = git_repos[:CAP]
        for r in shown:
            lines.append("- " + r["repo"] + " (" + str(r["count"]) + "): "
                         + r["latest_subject"])
        remainder = len(git_repos) - len(shown)
        if remainder > 0:
            lines.append("- and " + str(remainder) + " more repos")
    lines.append("")

    lines.append("## Launchd health")
    if not launchd["ok"]:
        lines.append(launchd["reason"] + ".")
    else:
        lines.append(str(launchd["failures"])
                      + " loaded jobs with a nonzero last exit.")
    lines.append("")

    lines.append("## Vault notes added")
    if not vault["ok"]:
        lines.append(vault["reason"] + ".")
    else:
        lines.append(str(vault["count"]) + " notes added.")
        if vault["top"]:
            lines.extend(_bullet_list(vault["top"]))

    return "\n".join(lines) + "\n"


def main():
    day = now_utc().strftime("%Y-%m-%d")
    board = board_deltas()
    crm = crm_deltas()
    git_repos = git_activity()
    launchd = launchd_health()
    vault = vault_notes_added()

    body = render(day, board, crm, git_repos, launchd, vault)

    print(body)
    if DRY_RUN:
        print("(dry-run: not logging to vault)", file=sys.stderr)
        return 0

    note_id = "digest-" + day
    result = subprocess.run(
        [sys.executable, "-m", "notes_vault", "log",
         "--title", "Morning digest, " + day,
         "--body", body,
         "--tag", "digest",
         "--source", "claude.cli",
         "--id", note_id,
         "--db", VAULT_DB],
        cwd=VAULT_REPO, capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
