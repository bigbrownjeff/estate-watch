#!/bin/bash
# Offline tests for prompt-maestro-weekly.sh's completion contract (board#326:
# assert a real diff on best-practices.md, assert the branch was actually
# pushed, and write $REPORT inside the run's own worktree instead of the
# canonical ~/Projects/personas checkout). No real subprocess ever touches the
# network, ~/.claude, or ~/Projects/personas -- everything is a local git repo
# and stub binary under a per-test $WORK dir.
#
# Run: bash ~/.claude/bin/tests/test_prompt_maestro_weekly.sh
set -u
SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/prompt-maestro-weekly.sh"
[ -f "$SCRIPT" ] || SCRIPT="$HOME/.claude/bin/prompt-maestro-weekly.sh"
PASS=0
FAIL=0
check() {
  if [ "$2" = "1" ]; then PASS=$((PASS+1)); echo "ok   - $1"
  else FAIL=$((FAIL+1)); echo "FAIL - $1${3:+ :: $3}"; fi
}

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# ---- fake $HOME/.claude/bin (runlog, failtask) --------------------------------
FAKE_HOME="$WORK/home"
mkdir -p "$FAKE_HOME/.claude/bin"
cat > "$FAKE_HOME/.claude/bin/runlog" <<'EOS'
#!/bin/bash
exec "$@"
EOS
chmod +x "$FAKE_HOME/.claude/bin/runlog"

cat > "$FAKE_HOME/.claude/bin/failtask" <<EOS
#!/bin/bash
echo "\$*" >> "$WORK/failtask.log"
exit 0
EOS
chmod +x "$FAKE_HOME/.claude/bin/failtask"

# ---- fake origin (bare) + PERSONAS_REPO (canonical checkout) ------------------
ORIGIN="$WORK/origin.git"
git init -q --bare "$ORIGIN"

PERSONAS="$WORK/personas"
git init -q "$PERSONAS"
git -C "$PERSONAS" config user.email test@example.com
git -C "$PERSONAS" config user.name test
mkdir -p "$PERSONAS/prompt-maestro/research/weekly-reports"
echo "# best practices" > "$PERSONAS/prompt-maestro/research/best-practices.md"
git -C "$PERSONAS" add -A
git -C "$PERSONAS" commit -q -m init
git -C "$PERSONAS" branch -M primary
git -C "$PERSONAS" remote add origin "$ORIGIN"
git -C "$PERSONAS" push -q origin primary

# canonical checkout's dirty-state fingerprint -- must be untouched by every case
canonical_clean() {
  [ -z "$(git -C "$PERSONAS" status --porcelain)" ]
}

# claude-headless stub: reads PM_* env vars the real wrapper exports, then
# performs exactly the git choreography a given scenario calls for.
make_claude_headless() {
  local mode="$1"
  cat > "$FAKE_HOME/.claude/bin/claude-headless" <<EOS
#!/bin/bash
set -e
git -C "$PERSONAS" fetch origin -q
git -C "$PERSONAS" worktree add "\$PM_WORKTREE_DIR" -b "\$PM_BRANCH" origin/primary -q
cd "\$PM_WORKTREE_DIR"
git config user.email test@example.com
git config user.name test
mkdir -p "\$(dirname "\$PM_REPORT")"
case "$mode" in
  ok)
    echo "changed content" >> prompt-maestro/research/best-practices.md
    echo "# weekly report" > "\$PM_REPORT"
    git add -A
    git commit -q -m refresh
    git push -q origin "\$PM_BRANCH"
    ;;
  no_report)
    echo "changed content" >> prompt-maestro/research/best-practices.md
    git add -A
    git commit -q -m refresh
    git push -q origin "\$PM_BRANCH"
    ;;
  no_diff)
    echo "# weekly report" > "\$PM_REPORT"
    git add -A
    git commit -q -m "refresh (report only)"
    git push -q origin "\$PM_BRANCH"
    ;;
  no_push)
    echo "changed content" >> prompt-maestro/research/best-practices.md
    echo "# weekly report" > "\$PM_REPORT"
    git add -A
    git commit -q -m refresh
    ;;
esac
exit 0
EOS
  chmod +x "$FAKE_HOME/.claude/bin/claude-headless"
}

run_case() {
  local mode="$1"
  rm -f "$WORK/failtask.log"
  # Each case gets its own personas worktree registry entry to avoid branch-name
  # collisions (the real script derives the branch from today's date; tests hold
  # that fixed by construction, one fresh PERSONAS_REPO clone per case).
  make_claude_headless "$mode"
  HOME="$FAKE_HOME" RUNLOG="$FAKE_HOME/.claude/bin/runlog" \
    CLAUDE_HEADLESS="$FAKE_HOME/.claude/bin/claude-headless" \
    FAILTASK="$FAKE_HOME/.claude/bin/failtask" \
    PERSONAS_REPO="$PERSONAS" \
    zsh "$SCRIPT" >"$WORK/stdout.log" 2>"$WORK/stderr.log"
  echo $?
}

# ---- case 1: success -----------------------------------------------------
rc=$(run_case ok)
check "ok case exits 0" "$([ "$rc" = "0" ] && echo 1 || echo 0)"
check "ok case files no failtask" "$([ ! -s "$WORK/failtask.log" ] && echo 1 || echo 0)"
check "ok case: canonical personas checkout stays clean" "$(canonical_clean && echo 1 || echo 0)"
git -C "$PERSONAS" worktree prune
git -C "$PERSONAS" branch -D "$(date +%F | sed 's/^/chore\/prompt-maestro-refresh-/')" >/dev/null 2>&1
git push "$ORIGIN" --delete "$(date +%F | sed 's/^/chore\/prompt-maestro-refresh-/')" >/dev/null 2>&1 -q 2>/dev/null || \
  git -C "$PERSONAS" push origin --delete "$(date +%F | sed 's/^/chore\/prompt-maestro-refresh-/')" -q 2>/dev/null

# ---- case 2: no report written -------------------------------------------
rc=$(run_case no_report)
check "no_report case exits nonzero" "$([ "$rc" != "0" ] && echo 1 || echo 0)"
check "no_report case files a failtask" "$(grep -q 'wrote no report' "$WORK/failtask.log" 2>/dev/null && echo 1 || echo 0)"
git -C "$PERSONAS" worktree list --porcelain | grep '^worktree' | grep -v "^worktree $PERSONAS\$" | awk '{print $2}' | while read -r wt; do
  git -C "$PERSONAS" worktree remove --force "$wt" 2>/dev/null
done
git -C "$PERSONAS" worktree prune
BR="$(date +%F | sed 's/^/chore\/prompt-maestro-refresh-/')"
git -C "$PERSONAS" branch -D "$BR" >/dev/null 2>&1
git -C "$PERSONAS" push origin --delete "$BR" -q 2>/dev/null

# ---- case 3: report present, best-practices.md never changed -------------
rc=$(run_case no_diff)
check "no_diff case exits nonzero" "$([ "$rc" != "0" ] && echo 1 || echo 0)"
check "no_diff case files the diff-specific failtask" "$(grep -q 'no diff on best-practices.md' "$WORK/failtask.log" 2>/dev/null && echo 1 || echo 0)"
git -C "$PERSONAS" worktree list --porcelain | grep '^worktree' | grep -v "^worktree $PERSONAS\$" | awk '{print $2}' | while read -r wt; do
  git -C "$PERSONAS" worktree remove --force "$wt" 2>/dev/null
done
git -C "$PERSONAS" worktree prune
git -C "$PERSONAS" branch -D "$BR" >/dev/null 2>&1
git -C "$PERSONAS" push origin --delete "$BR" -q 2>/dev/null

# ---- case 4: committed but never pushed -----------------------------------
rc=$(run_case no_push)
check "no_push case exits nonzero" "$([ "$rc" != "0" ] && echo 1 || echo 0)"
check "no_push case files the push-specific failtask" "$(grep -q 'branch never reached origin' "$WORK/failtask.log" 2>/dev/null && echo 1 || echo 0)"
check "no_push case: canonical personas checkout still stays clean" "$(canonical_clean && echo 1 || echo 0)"

echo "---"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
