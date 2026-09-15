#!/bin/zsh
# Weekly prompt-engineering research refresh (launchd: com.jeffpinto.prompt-maestro-research).
# Headless Claude session as the prompt-maestro persona. Logs via runlog.
#
# Completion contract (board#326): a headless -p session that backgrounds work and
# "stands by" dies silently at exit, so completion is proven by three checks run
# against the SAME temporary worktree the refresh already commits+pushes from --
# never against the canonical ~/Projects/personas checkout, which is how that tree
# kept going dirty (board#338: every run wrote its report straight into the
# canonical checkout, outside any branch, and left it there uncommitted):
#   1. $REPORT (now written INSIDE the worktree) is non-empty.
#   2. best-practices.md actually changed on the refresh branch vs. its base.
#   3. the refresh branch actually reached origin (git ls-remote), not just a
#      local commit.
# All three would have caught the 2026-08-03/08-10 incident this card documents:
# commit 5c66002 was made but never pushed, and a later unrelated merge (#31)
# overwrote best-practices.md with an older copy while the report file (living
# outside any branch, in the canonical checkout) survived untouched -- so the
# report asserted four additions the doc no longer contained, for a week, with
# no signal.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

# Overridable for offline testing (local-tools/tests/test_prompt_maestro_weekly.sh);
# defaults are the real estate paths.
RUNLOG="${RUNLOG:-$HOME/.claude/bin/runlog}"
CLAUDE_HEADLESS="${CLAUDE_HEADLESS:-$HOME/.claude/bin/claude-headless}"
FAILTASK="${FAILTASK:-$HOME/.claude/bin/failtask}"
PERSONAS_REPO="${PERSONAS_REPO:-$HOME/Projects/personas}"

DATE="$(date +%F)"
BRANCH="chore/prompt-maestro-refresh-$DATE"
WORKTREE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/prompt-maestro-refresh.XXXXXX")"
REPORT="$WORKTREE_DIR/prompt-maestro/research/weekly-reports/$DATE.md"
BP_REL="prompt-maestro/research/best-practices.md"
FAILKEY="launchd:com.jeffpinto.prompt-maestro-research"

# Exported so the headless session's own tool calls (and the test stub) can read
# the exact paths this run expects, instead of re-parsing them out of the prompt.
export PM_WORKTREE_DIR="$WORKTREE_DIR" PM_BRANCH="$BRANCH" PM_REPORT="$REPORT" \
  PM_PERSONAS_REPO="$PERSONAS_REPO"

"$RUNLOG" "$CLAUDE_HEADLESS" -p "Adopt the prompt-maestro persona (installed at ~/.claude/agents/prompt-maestro.md; read and become it before anything else). Run the WEEKLY RESEARCH REFRESH: (1) search for new prompt-optimization / LLM-as-judge / prompt-format papers from the last ~10 days (arXiv cs.CL/cs.AI + targeted queries on judge prompts, prompt optimization, safety classification prompting); (2) verify anything you intend to add by fetching its arXiv page -- never invent a citation; (3) from a freshly-fetched $PERSONAS_REPO, create the worktree already prepared at $WORKTREE_DIR on branch $BRANCH (git -C $PERSONAS_REPO fetch origin && git -C $PERSONAS_REPO worktree add $WORKTREE_DIR -b $BRANCH origin/primary -- the directory already exists, empty); (4) make EVERY edit inside $WORKTREE_DIR -- update prompt-maestro/research/best-practices.md there with dated entries (additions, strength-of-evidence changes, demotions), and write the completion report there too (below); NEVER edit the canonical $PERSONAS_REPO checkout directly, its tree must stay clean; (5) if a new finding invalidates a technique an estate lane already shipped, file it via ~/.claude/bin/failtask for that lane's owner rather than silently editing; (6) commit everything in $WORKTREE_DIR -- best-practices.md AND the report together, in the same commit(s), so they can never drift apart again -- and push $BRANCH to origin. IMPORTANT: you are running headless one-shot -- the process ends the moment you stop; run every step SYNCHRONOUSLY, never background a task or stand by for a notification. The completion contract is writing a <=15-line summary (papers reviewed, entries changed, null week is fine) to $REPORT inside the worktree, committed and pushed with the branch -- do not end the session until that file exists AND the branch is confirmed pushed. If a harness usage warning appears, downgrade to a status-only report but still commit it and push."
rc=$?

fail() {
  "$FAILTASK" personas "$1" --detail "$2" --dedupe-key "$FAILKEY" || true
  echo "$1" >&2
  # Leave $WORKTREE_DIR in place on failure -- it's the evidence a human needs.
  exit 1
}

if [[ ! -s "$REPORT" ]]; then
  fail "FAILURE: prompt-maestro weekly refresh wrote no report" \
    "claude -p exited rc=$rc but $REPORT is missing/empty -- run did not complete (background-wait or mid-run death). See launchd.err + latest runlog. Worktree left at $WORKTREE_DIR for inspection."
fi

if ! git -C "$WORKTREE_DIR" rev-parse --verify HEAD >/dev/null 2>&1; then
  fail "FAILURE: prompt-maestro weekly refresh made no commit" \
    "Report exists at $REPORT but $WORKTREE_DIR is not a git worktree with a commit on it -- the refresh never committed. rc=$rc. Worktree left at $WORKTREE_DIR."
fi

MERGE_BASE="$(git -C "$WORKTREE_DIR" merge-base HEAD origin/primary 2>/dev/null || echo origin/primary)"
if git -C "$WORKTREE_DIR" diff --quiet "$MERGE_BASE" -- "$BP_REL" 2>/dev/null; then
  fail "FAILURE: prompt-maestro weekly refresh produced no diff on best-practices.md" \
    "Branch $BRANCH in $WORKTREE_DIR has a report at $REPORT but 'git diff $MERGE_BASE -- $BP_REL' shows no change -- the exact 08-03/08-10 failure pattern (report claims content the doc doesn't contain). rc=$rc. Worktree left at $WORKTREE_DIR."
fi

if ! git -C "$WORKTREE_DIR" ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  fail "FAILURE: prompt-maestro weekly refresh branch never reached origin" \
    "Branch $BRANCH was committed in $WORKTREE_DIR but 'git ls-remote --heads origin $BRANCH' found nothing -- the commit is local-only, the same root cause as commit 5c66002 in the 08-03 incident (push-every-commit rule). rc=$rc. Worktree left at $WORKTREE_DIR."
fi

echo "prompt-maestro weekly refresh OK: report=$REPORT branch=$BRANCH (pushed, best-practices.md changed)"
git -C "$PERSONAS_REPO" worktree remove --force "$WORKTREE_DIR" 2>/dev/null || rm -rf "$WORKTREE_DIR"
exit $rc
