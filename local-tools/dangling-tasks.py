#!/usr/bin/env python3
"""dangling-tasks — the handoff open-thread lint.

Every session handoff ends with an "Open threads / next steps" section. The
2026-08-28 sweep of a month of handoffs and transcripts found that section is
where tasks go to die: they are written down, nobody files them, and the next
session starts from the handoff prose instead of the board. This lint closes
that loop.

It parses every handoff written in the last N days (default 8, so the weekly
Sunday hygiene run always overlaps the previous run), pulls the open-thread
lines out, and checks each one against GitHub Project #1:

  1. an explicit `board:` / `board_item_id:` / PVTI_ / DI_ reference on the line
  2. a `handoff: <path>` line in some card body (how this lint stamps its own
     cards, so re-runs are idempotent)
  3. a normalized-title match against every board item at the 0.72 cutoff
     (same cutoff the 2026-08-27 tracker audit used to join the retired YAML)

Anything that matches none of the three is UNFILED and gets printed. With
--file it is filed through ~/.claude/bin/failtask, which owns the taxonomy
(Project/Group from the alias map, Owner, Status, failkey dedupe), so this
script never touches the GitHub API directly for writes.

Report mode is the default and is what the weekly projects-hygiene job runs.

Usage:
  dangling-tasks.py [--days N] [--file] [--json] [--no-cache]

Exit code is always 0: a lint must never break the job that calls it.
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta

HOME = os.path.expanduser("~")
CACHE = os.path.join(HOME, ".claude", "failures", "board-cache.json")
FAILTASK = os.path.join(HOME, ".claude", "bin", "failtask")
CACHE_TTL = 900  # s
CUTOFF = 0.72

# T-1742: the private repo real Issues live in. Single constant, same override
# convention as failtask's FAILTASK_BOARD_REPO / decide's DECIDE_BOARD_REPO, so
# a scratch-repo dry run doesn't need a code edit. Read into the regex below,
# never hardcoded there.
BOARD_REPO = os.environ.get("DANGLING_TASKS_BOARD_REPO", "bigbrownjeff/board")
_repo_owner, _, _repo_name = BOARD_REPO.partition("/")

# The headings a handoff parks unfinished work under. Kept deliberately wide:
# a false positive costs one line of report, a false negative costs a task.
HEAD_RE = re.compile(
    r"^\s{0,3}#{1,6}\s*.*(open threads?|next steps?|left undone|not done|needs jeff|"
    r"decisions? for jeff|jeff-only|jeff only|your call|follow[- ]?ups?|outstanding|"
    r"still open|deferred|todo|to do|what'?s left|remaining|blocked on|awaiting)",
    re.I,
)
ANY_HEAD_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
INLINE_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])?\s*(?:TODO|FIXME)\b[:\-]\s*(.*\S)\s*$", re.I)
# Qualified references only (review fix 7). A bare `#123` is ambiguous with a
# PR number in any of the estate's other repos (a 2026-08-29 handoff cites
# `#294`, a PR in outbound_with_jeff_and_marv) and must NEVER match: turning
# that false positive into a false negative would be silent and permanent,
# the opposite of what this lint exists to catch. Only three shapes qualify:
# a bare PVTI_/DI_/I_ node id, `board#N` (optionally `owner/board#N`), or a
# full issue URL for the board repo. An issue/PR URL for any other repo, or a
# PR URL for the board repo, does not match.
BOARD_REF_RE = re.compile(
    r"\b(?P<id>PVTI_[A-Za-z0-9_-]+|DI_[A-Za-z0-9_-]+|I_[A-Za-z0-9_-]+)\b"
    r"|\b(?:%s/)?%s#(?P<num1>\d+)\b"
    r"|https://github\.com/%s/%s/issues/(?P<num2>\d+)\b"
    % (re.escape(_repo_owner), re.escape(_repo_name),
       re.escape(_repo_owner), re.escape(_repo_name))
)
DONE_RE = re.compile(r"^\s*(?:[-*+]\s*)?\[[xX]\]|^\s*(?:DONE|SHIPPED|MERGED|CLOSED)\b")
# Lines that are commentary, not tasks.
NOISE_RE = re.compile(
    r"^(none|nothing|n/?a|no open threads?|all clear|see (above|below)|"
    r"\(none\)|—|-)\.?$",
    re.I,
)
# An open thread is an ACTION someone still owes. Without this gate the lint
# scrapes every narrative bullet under a "Next steps" heading and reports ~290
# lines a week, which is the same as reporting nothing (2026-08-28 first run).
ACTION_RE = re.compile(
    r"\b(add|apply|ask|await|awaiting|blocked|book|build|buy|call|check|choose|"
    r"clean|close|commit|confirm|create|cut|decide|decision|delete|deploy|draft|"
    r"enable|extend|file|finish|fix|follow[- ]?up|hold|implement|land|launch|"
    r"merge|migrate|move|needs?|open|pending|pick|port|publish|pull|push|re-?run|"
    r"rebuild|record|refresh|regenerate|register|remove|rename|reply|report|"
    r"restore|retire|review|rotate|run|schedule|send|set|ship|sign|split|stage|"
    r"still|submit|swap|switch|sync|test|todo|unblock|update|upgrade|verify|"
    r"waiting|wire|write|should|must|need to|next step)\b",
    re.I,
)
# Past-tense session narrative that happens to sit under a next-steps heading.
NARRATIVE_RE = re.compile(
    r"^\s*(memory (added|written)|landed as|shipped|merged as|note:|why:|"
    r"context:|background:|result:|outcome:|decision:|rationale:|"
    r"\w+ (was|were|has been|have been) (added|fixed|shipped|merged|done|closed))\b",
    re.I,
)

STOP = set("""a an the to of for and or in on at by with from into is are was were be been
this that these those it its as if then than so our we you your i my me he she they them
run runs ran make makes made get gets got do does did use uses used add adds added""".split())


def norm(s):
    s = re.sub(r"`[^`]*`", " ", s or "")
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)          # md links -> text
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"^\[[a-z0-9_.-]+\]\s*", "", s, flags=re.I)  # leading [project] tag
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    toks = [t for t in s.split() if t and t not in STOP]
    return " ".join(toks)


FNAME_DATE_RE = re.compile(r"(20\d\d)-(\d\d)-(\d\d)")


def handoff_date(path):
    """The handoff's own date, not its mtime.

    Handoff filenames start with the session date. mtime lies: a git checkout,
    a repo move, or an unrelated edit re-dates a June note into today's window
    (2026-08-28: a `--days 1` run surfaced a 2026-06-10 RVC handoff this way).
    Fall back to mtime only when the filename carries no date.
    """
    m = FNAME_DATE_RE.search(os.path.basename(path))
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).timestamp()
        except ValueError:
            pass
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def handoff_files(days):
    """Every handoff written in the window, worktree copies excluded."""
    cutoff = time.time() - days * 86400
    roots = [os.path.join(HOME, "Projects"), os.path.join(HOME, ".claude", "handoffs")]
    skip = ("/_wt/", "/.worktrees/", "/_worktrees/", "-wt/", "-worktrees/", "/.wt/",
            "/.claude/worktrees/", "/node_modules/")
    out = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            if "/.claude/handoffs" not in dirpath and not dirpath.endswith(
                    os.path.join(".claude", "handoffs")):
                if not dirpath.startswith(os.path.join(HOME, ".claude", "handoffs")):
                    continue
            if any(s in dirpath + "/" for s in skip):
                continue
            for fn in filenames:
                if not fn.endswith(".md"):
                    continue
                p = os.path.join(dirpath, fn)
                if handoff_date(p) >= cutoff:
                    out.append(p)
    return sorted(set(out))


PROJ_HDR_RE = re.compile(r"\*\*Project:\*\*\s*([A-Za-z0-9_./-]+)")


def project_of(path):
    """The project slug a handoff belongs to.

    The global ~/.claude/handoffs tree holds notes for every project, so the
    directory name is useless there: read the template's `**Project:**` header
    first and only fall back to the path. failtask's alias map handles anything
    unrecognized by dropping it on `other`.
    """
    if path.startswith(os.path.join(HOME, ".claude", "handoffs")):
        try:
            with open(path, errors="replace") as f:
                head = f.read(2000)
            m = PROJ_HDR_RE.search(head)
            if m:
                return os.path.basename(m.group(1).rstrip("/"))
        except OSError:
            pass
        return "infra"
    parts = path.split(os.sep)
    try:
        i = parts.index("Projects")
        return parts[i + 1]
    except (ValueError, IndexError):
        return "other"


def extract(path):
    """[(text, board_ref_or_None)] — the open-thread lines in one handoff."""
    try:
        with open(path, errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    hits, in_sec, fence = [], False, False
    for ln in lines:
        if ln.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        if ANY_HEAD_RE.match(ln):
            in_sec = bool(HEAD_RE.match(ln))
            continue
        m = INLINE_RE.match(ln)
        if not m and in_sec:
            m = BULLET_RE.match(ln)
        if not m:
            continue
        text = m.group(1).strip()
        if DONE_RE.match(ln) or NOISE_RE.match(text) or len(norm(text)) < 12:
            continue
        if NARRATIVE_RE.match(re.sub(r"[*_`]", "", text)) or not ACTION_RE.search(text):
            continue
        hits.append((text[:400], board_ref(text)))
    return hits


def board_ref(text):
    """(kind, value) for the first qualified board reference in text, or None.

    kind "id" carries a bare content/item node id (PVTI_/DI_/I_), checked
    against the live `ids` set. kind "issue" carries an int issue number,
    checked against the board repo's own issue numbers only — a `board#N`
    or issue-URL match never counts a number filed against a different repo.
    """
    m = BOARD_REF_RE.search(text)
    if not m:
        return None
    if m.group("id"):
        return ("id", m.group("id"))
    num = m.group("num1") or m.group("num2")
    return ("issue", int(num))


def board_issue_numbers(items, board_repo):
    """Issue numbers that resolve to board_repo specifically.

    A `board#N` or board-repo issue URL only counts as filed if N is really
    one of ours (C-10/review fix 7) — never a same-numbered issue in the 4
    already-converted bigbrownjeff/lantern items, or anywhere else.
    """
    prefix = "https://github.com/%s/issues/" % board_repo
    nums = set()
    for i in items:
        url = (i.get("content") or {}).get("url") or ""
        if url.startswith(prefix):
            m = re.search(r"/issues/(\d+)$", url)
            if m:
                nums.add(int(m.group(1)))
    return nums


def ref_covered(ref, ids, issue_nums):
    """True if the (kind, value) ref from board_ref() is already on the board."""
    if not ref:
        return False
    kind, val = ref
    return (kind == "id" and val in ids) or (kind == "issue" and val in issue_nums)


def board_items(use_cache=True):
    if use_cache and os.path.exists(CACHE):
        try:
            if time.time() - os.path.getmtime(CACHE) < CACHE_TTL:
                d = json.load(open(CACHE))
                if d.get("v") == 2:
                    return d.get("items") or [], "cache"
        except Exception:
            pass
    gh = next((c for c in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh")
               if os.access(c, os.X_OK)), None)
    if not gh:
        return [], "no-gh"
    try:
        r = subprocess.run([gh, "project", "item-list", "1", "--owner", "bigbrownjeff",
                            "--limit", "1000", "--format", "json"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            # Fall back to a stale cache rather than reporting an empty board:
            # an empty board would mark every open thread unfiled and storm.
            if os.path.exists(CACHE):
                d = json.load(open(CACHE))
                return d.get("items") or [], "stale-cache"
            return [], "gh-failed"
        items = json.loads(r.stdout).get("items") or []
        try:
            json.dump({"v": 2, "items": items}, open(CACHE, "w"))
        except Exception:
            pass
        return items, "live"
    except Exception:
        if os.path.exists(CACHE):
            try:
                return json.load(open(CACHE)).get("items") or [], "stale-cache"
            except Exception:
                pass
        return [], "gh-error"


def fold_comment_bodies(item):
    """Comment bodies for item, joined, or "" — tolerant of a missing/odd shape.

    The item shape this reads is the one `ops-ui-design.md` §1 describes for
    `gh_board.py`'s COLUMNS (`comments`: null / "[]" / a JSON list of {body}
    dicts). Never raises: a malformed field degrades to no fold, not a crash.
    """
    comments = item.get("comments")
    if isinstance(comments, str):
        try:
            comments = json.loads(comments)
        except (TypeError, ValueError):
            return ""
    if not isinstance(comments, list):
        return ""
    return " ".join((c.get("body") or "") for c in comments if isinstance(c, dict))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=8)
    ap.add_argument("--file", action="store_true", help="file the unfiled ones via failtask")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--show-per", type=int, default=6,
                    help="lines printed per handoff in report mode")
    ap.add_argument("--max-file", type=int, default=25,
                    help="board-storm guard on --file")
    ap.add_argument("--fold-comments", action="store_true",
                    help="fold each item's comments (if the board cache carries "
                         "them) into its coverage token set. Off by default: "
                         "K-07 keeps provenance in card bodies, so this is "
                         "insurance for if that ever changes, not today's path.")
    a = ap.parse_args()

    files = handoff_files(a.days)
    if not files:
        print("dangling-tasks: no handoffs dated in the last %d days. "
              "That means nothing was handed off, not that nothing is open."
              % a.days)
        return 0
    items, src = board_items(use_cache=not a.no_cache)
    if not items:
        print("dangling-tasks: BOARD UNREADABLE (%s) — cannot lint, not the same as clean."
              % src)
        return 0

    ids = {i.get("id") for i in items} | {
        (i.get("content") or {}).get("id") for i in items}
    issue_nums = board_issue_numbers(items, BOARD_REPO)
    titles = [(norm((i.get("content") or {}).get("title") or i.get("title") or ""), i)
              for i in items]
    titles = [(t, i) for t, i in titles if t]
    # A card filed FROM a handoff carries a rewritten imperative title, not the raw
    # bullet, so title similarity alone does not recognise the sweep's own output
    # (2026-08-28: 86 freshly filed cards still read as unfiled). Index each card's
    # title+body token set, and the handoff paths it cites, and credit a line whose
    # tokens are contained in a card that cites the same handoff.
    cards = []
    for i in items:
        c = i.get("content") or {}
        blob = (c.get("title") or i.get("title") or "") + " " + (c.get("body") or "")
        if a.fold_comments:
            # C-11, off by default: only ever a no-op today (the board cache
            # this lint reads carries no "comments" field), and cheap
            # insurance for later if some provenance ever moves off the body.
            blob += " " + fold_comment_bodies(i)
        cards.append((set(norm(blob).split()),
                      set(re.findall(r"handoff:\s*(\S+)", c.get("body") or ""))))

    unfiled, covered, seen = [], 0, set()
    for p in files:
        stem = os.path.basename(p)[:-3]
        for text, ref in extract(p):
            key = norm(text)
            if (p, key) in seen:
                continue
            seen.add((p, key))
            if ref_covered(ref, ids, issue_nums):
                covered += 1
                continue
            ktok = set(key.split())
            if ktok:
                hit = False
                for ctok, chandoffs in cards:
                    if not ctok:
                        continue
                    cont = len(ktok & ctok) / len(ktok)
                    # same handoff cited: a loose match is enough. Otherwise demand
                    # near-total containment before calling a line already tracked.
                    if (p in chandoffs and cont >= 0.70) or cont >= 0.90:
                        hit = True
                        break
                if hit:
                    covered += 1
                    continue
            # Bullets are sentences, card titles are phrases: score the whole
            # line AND its first clause, keep the better of the two.
            clause = norm(re.split(r"[—:(\u2014]|\s-\s", text)[0]) or key
            best, score = None, 0.0
            for t, it in titles:
                sc = max(difflib.SequenceMatcher(None, key, t).ratio(),
                         difflib.SequenceMatcher(None, clause, t).ratio())
                if sc > score:
                    best, score = it, sc
            if score >= CUTOFF:
                covered += 1
                continue
            unfiled.append({
                "handoff": p, "stem": stem, "project": project_of(p), "text": text,
                "nearest": (best.get("content") or {}).get("title") if best else None,
                "score": round(score, 2),
            })

    show_per = a.show_per
    if a.json:
        print(json.dumps({"scanned": len(files), "board_source": src,
                          "covered": covered, "unfiled": unfiled}, indent=1))
    else:
        print("dangling-tasks: %d handoff(s) in the last %d days, board via %s, "
              "%d open thread(s) already on the board, %d UNFILED"
              % (len(files), a.days, src, covered, len(unfiled)))
        if unfiled:
            print("An open thread with no board card is a handoff defect. "
                  "File them with: dangling-tasks.py --days %d --file" % a.days)
        # Grouped by handoff: the defect is the handoff, not the individual line.
        by_ho = {}
        for u in unfiled:
            by_ho.setdefault(u["handoff"], []).append(u)
        for ho in sorted(by_ho, key=lambda h: -len(by_ho[h])):
            rows = by_ho[ho]
            print("\n  %s  [%s] — %d unfiled" % (ho, rows[0]["project"], len(rows)))
            for u in rows[:show_per]:
                print("      - %s" % u["text"][:150])
                if u["score"] >= 0.55:
                    print("        nearest card %.2f: %s"
                          % (u["score"], (u["nearest"] or "-")[:90]))
            if len(rows) > show_per:
                print("      ... %d more (use --json for the full list)"
                      % (len(rows) - show_per))

    if a.file and unfiled:
        if len(unfiled) > a.max_file:
            print("dangling-tasks: %d unfiled exceeds --max-file %d; filing the first "
                  "%d only. Re-run after triaging, or raise the cap deliberately."
                  % (len(unfiled), a.max_file, a.max_file))
            unfiled = unfiled[:a.max_file]
        for u in unfiled:
            key = "dangling:" + hashlib.sha1(
                ("%s|%s" % (u["stem"], norm(u["text"]))).encode()).hexdigest()[:12]
            title = re.sub(r"\s+", " ", u["text"])[:110]
            # A handoff's open-thread list is a SNAPSHOT of one session's end
            # state, and a later session often closes the thread while the old
            # handoff still reads "open". 77 cards were filed on 2026-08-28
            # from such snapshots and sat unreconciled for eighteen days; two
            # of them had been RULED before the card existed (the outreach link
            # style, ruled 2026-08-09; a slug-mint task superseded by
            # mint-at-accept). So every card carries the age of the prose it
            # came from and a date by which someone must check it against
            # reality. An automatic "was this resolved" test was tried on
            # 2026-09-15 and deliberately NOT shipped: keyword matching over
            # handoff prose either returned unrelated lines or nothing at all,
            # and a hint that reads like a receipt is worse than none.
            ho_date = handoff_date(u["handoff"])
            # handoff_date returns a POSIX timestamp, not a datetime.
            ho_str = (datetime.fromtimestamp(ho_date).date().isoformat()
                      if ho_date else "unknown")
            reconcile_by = (datetime.now() + timedelta(days=14)).date().isoformat()
            detail = ("Open thread left unfiled by a session handoff.\n\n"
                      "%s\n\nhandoff: %s\nhandoff-date: %s\n"
                      "filed-by: dangling-tasks lint\nreconcile-by: %s\n\n"
                      "RECONCILE BEFORE WORKING THIS. The line above is a snapshot of "
                      "what one session thought was open on %s. Check later sessions, "
                      "docs, rulings and live state first: the thread may already be "
                      "closed, superseded, or no longer wanted. Close it with the "
                      "receipt, bump it, or route it to the persona that owns it."
                      % (u["text"], u["handoff"], ho_str, reconcile_by, ho_str))
            try:
                subprocess.run([FAILTASK, u["project"], title, "--detail", detail,
                                "--dedupe-key", key, "--severity", "warn",
                                "--label", "OPEN THREAD"],
                               timeout=120)
            except Exception as e:
                print("dangling-tasks: failtask error for %r: %s" % (title[:60], e))
            time.sleep(1.5)  # board pacing
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # a lint must never break its caller
        print("dangling-tasks: FAILED: %r" % e)
        sys.exit(0)
