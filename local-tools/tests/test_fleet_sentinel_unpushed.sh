#!/bin/bash
# Regression test for fleet-sentinel.sh's repo-unpushed check (board #987/#242):
# a branch GitHub squash-merged (or whose commits already landed under a
# different remote ref) must never be counted as unpushed just because it has
# no upstream and diverges from the default branch -- only commits that are
# NOT reachable from ANY remote ref are real, loud, unpushed work.
#
# Builds two throwaway repos (each with its own bare "origin") in a temp dir:
#   repo-safe  -- a branch pushed to origin, then squash-merged into main
#                 locally (so it is not an ancestor of main and has no
#                 upstream), but its own remote-tracking ref still exists.
#                 Must NOT produce a repo-unpushed finding.
#   repo-loud  -- a branch with real commits that were never pushed anywhere.
#                 Must produce a repo-unpushed finding naming its commit count.
#
# Run: bash ~/.claude/bin/tests/test_fleet_sentinel_unpushed.sh
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$HERE/fleet-sentinel.sh"
[ -f "$SCRIPT" ] || SCRIPT="$HOME/.claude/bin/fleet-sentinel.sh"

PASS=0
FAIL=0
check() {
  if [ "$2" = "1" ]; then PASS=$((PASS+1)); echo "ok   - $1"
  else FAIL=$((FAIL+1)); echo "FAIL - $1${3:+ :: $3}"; fi
}

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

FAKE_HOME="$WORK/home"
mkdir -p "$FAKE_HOME/.claude/failures" "$FAKE_HOME/Projects"
echo '{}' > "$FAKE_HOME/.claude/failures/sentinel-config.json"

git_q() { git -C "$1" "${@:2}" >/dev/null 2>&1; }

# ---- repo-safe: squash-merged branch, already on a remote, no upstream ----
mkdir -p "$WORK/origin-safe.git"
git init -q --bare "$WORK/origin-safe.git"

SAFE="$FAKE_HOME/Projects/repo-safe"
git init -q "$SAFE"
git -C "$SAFE" config user.email test@example.com
git -C "$SAFE" config user.name test
echo one > "$SAFE/f.txt"
git -C "$SAFE" add -A
git -C "$SAFE" commit -q -m init
git -C "$SAFE" branch -M main
git -C "$SAFE" remote add origin "$WORK/origin-safe.git"
git_q "$SAFE" push -u origin main

git_q "$SAFE" checkout -b feature
echo two >> "$SAFE/f.txt"
git -C "$SAFE" commit -q -am "feature work"
git_q "$SAFE" push origin feature      # feature's commit now lives on origin too

git_q "$SAFE" checkout main
echo two >> "$SAFE/f.txt"
git -C "$SAFE" commit -q -am "feature work (squashed)"   # simulates GitHub squash-merge:
                                                          # a NEW commit on main, feature's
                                                          # tip is never its ancestor
git_q "$SAFE" push origin main
git_q "$SAFE" branch --unset-upstream feature            # squash-merge leaves it "no upstream"

# ---- repo-loud: real commits that never reached any remote ----
mkdir -p "$WORK/origin-loud.git"
git init -q --bare "$WORK/origin-loud.git"

LOUD="$FAKE_HOME/Projects/repo-loud"
git init -q "$LOUD"
git -C "$LOUD" config user.email test@example.com
git -C "$LOUD" config user.name test
echo one > "$LOUD/f.txt"
git -C "$LOUD" add -A
git -C "$LOUD" commit -q -m init
git -C "$LOUD" branch -M main
git -C "$LOUD" remote add origin "$WORK/origin-loud.git"
git_q "$LOUD" push -u origin main

git_q "$LOUD" checkout -b wip
echo two >> "$LOUD/f.txt"
git -C "$LOUD" commit -q -am "wip 1"
echo three >> "$LOUD/f.txt"
git -C "$LOUD" commit -q -am "wip 2"
git_q "$LOUD" checkout main   # clean HEAD, matches the dirty-checkout check's expectations

# ---- run the real check_
OUT=$(HOME="$FAKE_HOME" "$SCRIPT" --dry-run 2>&1)

echo "$OUT" | grep -q "repo-safe.*repo-unpushed\|\[repo-safe\] repo-unpushed"
check "repo-safe (squash-merged, no upstream) is NOT reported as unpushed" "$([ $? -ne 0 ] && echo 1 || echo 0)" "$(echo "$OUT" | grep 'repo-safe' | grep -i unpushed)"

echo "$OUT" | grep -q "\[repo-loud\] repo-unpushed .* repo-loud: 2 commit(s) exist"
check "repo-loud (2 genuinely unpushed commits) IS reported, with the right count" "$([ $? -eq 0 ] && echo 1 || echo 0)" "$(echo "$OUT" | grep 'repo-loud' | grep -i unpushed)"

echo "----"
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
