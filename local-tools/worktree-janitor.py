#!/usr/bin/env python3
"""worktree-janitor — remove linked worktrees whose work is finished, report the rest.

Why: orphaned worktrees accrue faster than anyone cleans them (2026-07-11 audit found 17;
they were back by 07-24). The post-merge cleanup CLAUDE.md prescribes — `git worktree
remove` + `git branch -d` — is the step that gets skipped when a session ends, so it should
not depend on a session remembering.

A worktree is removed ONLY when all of these hold, which together mean "nothing here exists
anywhere else":
  * it is a linked worktree, never the primary checkout
  * the tree is clean (no modifications, no untracked files)
  * it has an upstream and no unpushed commits
  * its branch is merged — a merged PR, or an ancestor of the default branch

Anything failing a check is reported, never touched. Untracked files in particular are
someone's uncommitted work: `worktree remove --force` would delete them, so this never
passes --force.

  worktree-janitor.py --dry-run     # show what would happen
  worktree-janitor.py --auto-safe   # act, but only on the class below; report the rest
  worktree-janitor.py               # act on everything that passes the checks

--auto-safe is what the scheduled job runs. It is the same checks plus a longer quiet
window: a worktree must have been untouched for AUTO_QUIET_S, not merely ACTIVE_WINDOW_S,
before an unattended run may remove it. A human previewing with --dry-run is deciding now
and can see the whole estate; a cron job at 09:15 cannot, so it waits a day longer. Reason
(2026-09-02): a weekly dry-run report removes nothing, so 58 merged lantern-cloud worktrees
piled up in the three days after the last run and were cleared by hand.
"""
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
ROOT = os.path.join(HOME, "Projects")
DRY = "--dry-run" in sys.argv[1:]
AUTO_SAFE = "--auto-safe" in sys.argv[1:]
ACTIVE_WINDOW_S = 6 * 3600   # touched this recently => a live lane, hands off
AUTO_QUIET_S = 24 * 3600     # an unattended run waits a day longer than a human would


def git(args, cwd=None, timeout=60):
    try:
        p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return 1, "", repr(e)


def primary_repos():
    """Primary checkouts only — a linked worktree has a .git FILE, not a directory."""
    out = []
    for name in sorted(os.listdir(ROOT)):
        d = os.path.join(ROOT, name)
        if os.path.isdir(os.path.join(d, ".git")):
            out.append(d)
    return out


def default_branch(repo):
    rc, out, _ = git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=repo)
    if rc == 0 and out:
        return out.split("/", 1)[-1]
    for cand in ("main", "primary", "master"):
        rc, _, _ = git(["rev-parse", "--verify", "origin/" + cand], cwd=repo)
        if rc == 0:
            return cand
    return None


def pr_merged(repo, branch):
    try:
        p = subprocess.run(["gh", "pr", "list", "--head", branch, "--state", "merged",
                            "--json", "number", "-q", ".[].number"],
                           cwd=repo, capture_output=True, text=True, timeout=60)
        return bool(p.stdout.strip())
    except Exception:
        return False


def worktrees(repo):
    """[(path, branch)] for LINKED worktrees only."""
    rc, out, _ = git(["worktree", "list", "--porcelain"], cwd=repo)
    if rc != 0:
        return []
    entries, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line:
            if cur.get("worktree"):
                entries.append(cur)
            cur = {}
        elif " " in line:
            k, v = line.split(" ", 1)
            cur[k] = v
        else:
            cur[line] = True
    res = []
    for e in entries[1:]:   # entry 0 is the primary checkout
        br = e.get("branch", "")
        res.append((e["worktree"], br.replace("refs/heads/", "") if br else None))
    return res


def launchd_paths():
    """Worktree paths a LaunchAgent runs from. Removing one breaks the job silently
    (2026-08-22: photo-atlas-ops, home of com.jeffpinto.photo-atlas-snapshot, was
    removed as 'merged, clean, pushed' — all true, and all beside the point)."""
    import glob
    refs = set()
    for plist in glob.glob(os.path.expanduser("~/Library/LaunchAgents/com.jeffpinto.*.plist")):
        try:
            body = open(plist).read()
        except OSError:
            continue
        for m in re.finditer(r"<string>(/Users/[^<]+)</string>", body):
            refs.add(m.group(1))
    return refs


def main():
    removed, kept = [], []
    launchd_refs = launchd_paths()
    for repo in primary_repos():
        wts = worktrees(repo)
        if not wts:
            continue
        default = default_branch(repo)
        for path, branch in wts:
            name = path.replace(HOME, "~")
            if not os.path.isdir(path):
                kept.append((name, "path missing — run `git worktree prune`"))
                continue
            if any(ref == path or ref.startswith(path + "/") for ref in launchd_refs):
                kept.append((name, "a LaunchAgent runs from this path — never remove"))
                continue
            if not branch:
                kept.append((name, "detached HEAD — decide by hand"))
                continue
            rc, dirt, _ = git(["status", "--porcelain"], cwd=path)
            if rc != 0:
                kept.append((name, "git status failed"))
                continue
            if dirt:
                kept.append((name, "%d uncommitted path(s) — someone's work" % len(dirt.splitlines())))
                continue
            rc, ahead, _ = git(["log", "--oneline", "@{upstream}..HEAD"], cwd=path)
            if rc != 0:
                kept.append((name, "no upstream — push or rescue-push before cleanup"))
                continue
            if ahead:
                kept.append((name, "%d unpushed commit(s)" % len(ahead.splitlines())))
                continue
            # An agent that just merged its PR may still be working in the worktree for a
            # follow-up task; pulling it out from under a live lane is worse than waiting
            # a week. Recent activity = hands off.
            rc, when, _ = git(["log", "-1", "--format=%ct"], cwd=path)
            window = AUTO_QUIET_S if AUTO_SAFE else ACTIVE_WINDOW_S
            idle = min(time.time() - int(when) if (rc == 0 and when.isdigit()) else 1e12,
                       time.time() - os.path.getmtime(path))
            if idle < window:
                kept.append((name, "active in the last %dh — leaving it alone"
                             % (window // 3600)))
                continue
            merged = pr_merged(repo, branch)
            if not merged and default:
                rc, _, _ = git(["merge-base", "--is-ancestor", "HEAD", "origin/" + default],
                               cwd=path)
                merged = (rc == 0)
            if not merged:
                kept.append((name, "branch %s not merged yet" % branch))
                continue
            if DRY:
                removed.append((name, "WOULD remove (branch %s merged, clean, pushed)" % branch))
                continue
            rc, _, err = git(["worktree", "remove", path], cwd=repo)
            if rc != 0:
                kept.append((name, "remove failed: %s" % err[:80]))
                continue
            git(["branch", "-d", branch], cwd=repo)
            git(["worktree", "prune"], cwd=repo)
            removed.append((name, "removed (branch %s merged)" % branch))

    verb = "would remove" if DRY else ("auto-removed" if AUTO_SAFE else "removed")
    print("worktree-janitor: %s %d, kept %d" % (verb, len(removed), len(kept)))
    for n, why in removed:
        print("  - %s: %s" % (n, why))
    if kept:
        print("  kept (untouched):")
        for n, why in kept:
            print("    * %s — %s" % (n, why))
    return 0


if __name__ == "__main__":
    unknown = [a for a in sys.argv[1:] if a not in ("--dry-run", "--apply", "--auto-safe")]
    if unknown:
        sys.stderr.write("worktree-janitor: unknown option(s) %s — nothing done.\n"
                         "usage: worktree-janitor.py [--dry-run|--apply|--auto-safe]\n"
                         "  (default: apply)\n" % unknown)
        sys.exit(2)
    sys.exit(main())
