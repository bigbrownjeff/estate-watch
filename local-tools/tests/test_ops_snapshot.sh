#!/bin/bash
# test_ops_snapshot -- prove local-tools/ops-snapshot.sh covers the estate and
# the offsite push cannot lie about its own result.
#
# Everything here runs through OPS_HOME / OPS_VAULT / OPS_REMOTE /
# OPS_MEMORY_REMOTE / OPS_MARKER / OPS_NO_FAILTASK pointed at a mktemp
# fixture home and an isolated RCLONE_CONFIG defining real crypt (and one
# plain) remotes over local temp directories (no network, no real
# credentials). The real ~/.claude, the real ~/ops-vault, the real
# ~/data-vaults/ops-vault marker, and the real gdw-crypt: remote are never
# touched -- HOME is redirected to the fixture home for every invocation, so
# even the script's own default OPS_MARKER path resolves inside the fixture.
#
# Style follows notes-vault/tests/test_data_snapshot.sh (ok/bad/check
# helpers, SUMMARY line, mutation-prove by editing a COPY and restoring from
# a cp'd backup -- never git checkout, never git stash). The subject under
# test is copied ONCE into the work dir before anything runs; every run_snap
# call and every mutation targets that copy. $SCRIPT itself (the file this
# repo tracks) is opened read-only and is never written to -- the final
# check below proves it byte for byte.
#
# Run: bash local-tools/tests/test_ops_snapshot.sh
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$HERE/ops-snapshot.sh"
[ -x "$SCRIPT" ] || [ -f "$SCRIPT" ] || { echo "FAIL: $SCRIPT not found"; exit 1; }
SCRIPT_HASH_BEFORE=$(shasum "$SCRIPT" | cut -d' ' -f1)

pass=0; fail=0
ok()   { pass=$((pass+1)); echo "  ok   - $1"; }
bad()  { fail=$((fail+1)); echo "  FAIL - $1"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$3', got '$2')"; fi; }

if ! command -v rclone >/dev/null 2>&1; then
  echo "SKIP: rclone not installed, cannot exercise this suite's offsite checks"
  echo "SUMMARY: 0 passed, 0 failed"
  exit 0
fi

WORK=$(mktemp -d) || exit 1
trap 'rm -rf "$WORK"' EXIT

# ---- the subject under test: a COPY, made once, never $SCRIPT itself ----
# T1 (P1): the suite must never write to the file this repo tracks. Every
# run and every mutation below targets $SUBJECT; $BACKUP is the pristine
# copy every mutation restores from (cp, never git checkout/stash).
SUBJECT="$WORK/ops-snapshot.subject.sh"
cp "$SCRIPT" "$SUBJECT"
chmod +x "$SUBJECT"
BACKUP="$WORK/ops-snapshot.orig.sh"
cp "$SCRIPT" "$BACKUP"

# ---- isolated rclone config: a real crypt remote over a local backing dir,
# plus a non-crypt remote for the crypt-refusal cases. No network, no real
# credentials. ----
BACKING="$WORK/backing"
PLAINBACKING="$WORK/plainbacking"
mkdir -p "$BACKING"
PW=$(rclone obscure test-pw-1)
PW2=$(rclone obscure test-pw-2)
RCONF="$WORK/rclone.conf"
cat > "$RCONF" <<CFG
[test-crypt]
type = crypt
remote = $BACKING
password = $PW
password2 = $PW2

[test-plain]
type = local
CFG
export RCLONE_CONFIG="$RCONF"

# ---- fixture home: only the allowlisted dirs (plus one deliberately
# missing, and non-allowlisted dirs that must never appear anywhere). ----
FAKE_HOME="$WORK/home"
mkdir -p \
  "$FAKE_HOME/.claude/handoffs" \
  "$FAKE_HOME/.claude/observability" \
  "$FAKE_HOME/.claude/jobs" \
  "$FAKE_HOME/.claude/scheduled" \
  "$FAKE_HOME/.claude/watch" \
  "$FAKE_HOME/.claude/state" \
  "$FAKE_HOME/.claude/secrets" \
  "$FAKE_HOME/.cloudflared" \
  "$FAKE_HOME/.local/other" \
  "$FAKE_HOME/.local/bin" \
  "$FAKE_HOME/Library/LaunchAgents" \
  "$FAKE_HOME/data-vaults/claude-memory"
# .claude/scheduled-tasks/ is deliberately NOT created: proves a missing
# allowlisted dir is skipped without error, not just an existing one landing.
echo "note one"        > "$FAKE_HOME/.claude/handoffs/note1.md"
echo "obs one"          > "$FAKE_HOME/.claude/observability/o1.txt"
echo "job one"          > "$FAKE_HOME/.claude/jobs/j1.txt"
echo "sched one"        > "$FAKE_HOME/.claude/scheduled/s1.txt"
echo "watch one"        > "$FAKE_HOME/.claude/watch/w1.txt"
echo "state one"        > "$FAKE_HOME/.claude/state/st1.txt"
echo "API_KEY=notreal"  > "$FAKE_HOME/.claude/secrets/apikey.txt"
echo "not-a-real-cert"  > "$FAKE_HOME/.cloudflared/cert.pem"
echo "not-a-real-env"   > "$FAKE_HOME/.env"
echo "not-a-real-gitignore" > "$FAKE_HOME/.gitignore_global"
echo "regular script"   > "$FAKE_HOME/.local/bin/real-script.sh"
chmod +x "$FAKE_HOME/.local/bin/real-script.sh"
echo "SYMLINK_TARGET_CANARY_TEXT" > "$FAKE_HOME/.local/other/realbin"
ln -s "$FAKE_HOME/.local/other/realbin" "$FAKE_HOME/.local/bin/claude"
echo "memory snapshot 1" > "$FAKE_HOME/data-vaults/claude-memory/mem1.bin"

# Positive-only allowlist of what may exist under $VAULT/claude and
# $VAULT/home after a run (and, mirrored, what may exist offsite). A
# denylist of the forbidden names would put "secrets" and ".env" as literal
# strings into this committed test for no reason; the allowlist form fails
# on anything unexpected instead, which is the case actually feared here.
CLAUDE_ALLOWED="bin hooks agents agent-memory skills commands failures links todos bundles handoffs-archive memory handoffs observability jobs scheduled scheduled-tasks watch state"
HOME_ALLOWED="local-bin .gitignore_global"

# T4 (P2): pure check -- prints the space-separated names present in $1 that
# are not in the allowlist $2, one per invocation, never registers ok/bad
# itself so the mutation-prove cases below can inspect its verdict without
# corrupting the pass/fail counters. Enumerated with find -mindepth 1
# -maxdepth 1 so dotfiles (.gitignore_global, a leaked .env) are included --
# the old `for entry in "$dir"/*` silently skipped every one of them.
allowlist_bad_names() {  # $1 dir  $2 space-separated allowed names
  local dir="$1" allowed=" $2 "
  [ -d "$dir" ] || return 0
  find "$dir" -mindepth 1 -maxdepth 1 2>/dev/null | while IFS= read -r entry; do
    [ -e "$entry" ] || continue
    local name; name=$(basename "$entry")
    case "$allowed" in
      *" $name "*) : ;;
      *) printf '%s ' "$name" ;;
    esac
  done
}
assert_allowlisted() {  # $1 dir  $2 allowed  $3 label
  local dir="$1" allowed="$2" label="$3" bad_names
  bad_names=$(allowlist_bad_names "$dir" "$allowed")
  if [ -z "$(printf '%s' "$bad_names" | tr -d '[:space:]')" ]; then
    ok "$label: only allowlisted names present"
  else
    bad "$label: unexpected name(s) present:$bad_names"
  fi
}
# T4: the same allowlist, asserted against the OFFSITE listing (rclone lsf),
# not just the local vault -- a leak the local check catches could still
# reach the remote if the two trees ever diverged.
# T13 (P2 pin): a dropped rc or an empty listing must FAIL, not pass vacuously --
# a listing command that errored, or a path that was silently wrong, proved
# nothing about what is actually offsite.
assert_allowlisted_remote() {  # $1 remote:path  $2 allowed  $3 label
  local remote="$1" allowed=" $2 " label="$3" bad_names="" name
  local rc listing
  listing=$(rclone lsf "$remote" 2>/dev/null)
  rc=$?
  if [ "$rc" -ne 0 ]; then
    bad "$label: rclone lsf failed (rc=$rc) against $remote, offsite listing not verified"
    return
  fi
  if [ -z "$(printf '%s' "$listing" | tr -d '[:space:]')" ]; then
    bad "$label: rclone lsf returned nothing for $remote, offsite listing not verified"
    return
  fi
  while IFS= read -r entry; do
    [ -z "$entry" ] && continue
    name="${entry%/}"
    case "$allowed" in
      *" $name "*) : ;;
      *) bad_names="$bad_names $name" ;;
    esac
  done <<EOF
$listing
EOF
  if [ -z "$bad_names" ]; then ok "$label: only allowlisted names present offsite"
  else bad "$label: unexpected name(s) present offsite:$bad_names"; fi
}

# Helper: keyed by the WHOLE fixture home tree so a script that reads only is
# provably a script that never writes. Symlinks are recorded by their target
# path, not dereferenced (a dangling one would break shasum).
snapshot_home() {  # $1 dir to walk  $2 optional exact path to exclude
  local exclude="${2:-}"
  find "$1" \( -type f -o -type l \) 2>/dev/null | sort | while read -r f; do
    [ -n "$exclude" ] && [ "$f" = "$exclude" ] && continue
    if [ -L "$f" ]; then echo "L $f -> $(readlink "$f")"
    else echo "F $f $(shasum "$f" 2>/dev/null | cut -d' ' -f1)"; fi
  done | shasum | cut -d' ' -f1
}

run_snap() {  # env vars set by caller (OPS_VAULT, OPS_REMOTE, OPS_MEMORY_REMOTE,
              # OPS_MARKER, OPS_HOME, OPS_MEMORY_VAULT_OFFSITE, RCLONE_DRY_RUN...)
  # T1/P2 escalation: HOME is always the fixture, regardless of OPS_HOME, so
  # FAILTASK ($HOME/.claude/bin/failtask) and the script's own default
  # OPS_MARKER ($HOME/data-vaults/ops-vault/last-ok.json) can never resolve
  # into the real $HOME. OPS_HOME defaults to the fixture too, unless a
  # caller overrides it (T6's missing-directory fixtures).
  OPS_HOME="${OPS_HOME:-$FAKE_HOME}" HOME="$FAKE_HOME" RCLONE_CONFIG="$RCONF" OPS_NO_FAILTASK=1 \
    /bin/bash "$SUBJECT" "$@" 2>&1
}

echo "== mutation helper: replace an exact block once, error if not exactly one match =="
mutate_block() {  # old-file new-file target-script
  python3 - "$1" "$2" "$3" <<'PY'
import sys
old = open(sys.argv[1]).read()
new = open(sys.argv[2]).read()
target = sys.argv[3]
src = open(target).read()
n = src.count(old)
if n != 1:
    sys.stderr.write("MUTATE-ERROR: expected 1 occurrence, found %d\n" % n)
    sys.exit(1)
open(target, "w").write(src.replace(old, new, 1))
PY
}
list_archived_paths() {  # $1 remote-path-of-replaced  $2 filename-suffix
  rclone lsjson --recursive "$1" 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    d = []
suffix = sys.argv[1]
for e in d:
    if e.get('Path', '').endswith(suffix):
        print(e['Path'])
" "$2"
}
restore_subject() {  # $1 label
  cp "$BACKUP" "$SUBJECT"
  diff -q "$BACKUP" "$SUBJECT" >/dev/null 2>&1 && ok "script restored from backup after $1 mutation" \
                                                || bad "script NOT restored after $1 mutation"
}

echo "== 1. first full run =="
VAULT1="$WORK/vault1"
MARK1="$WORK/vault1.marker.json"
# F7 writes the claude-memory offsite marker at its OWN default path,
# $FAKE_HOME/data-vaults/claude-memory-offsite/last-ok.json -- a sibling of, not
# inside, $FAKE_HOME/data-vaults/claude-memory (which F7 leaves untouched; T10
# below pins that). Excluded here on purpose, since its own content changing
# every run is correct behaviour, not a script that reads only. Everything else
# in the home tree must be inert.
MEM_MARKER_EXCLUDE="$FAKE_HOME/data-vaults/claude-memory-offsite/last-ok.json"
home_before=$(snapshot_home "$FAKE_HOME" "$MEM_MARKER_EXCLUDE")
OUT1=$(OPS_VAULT="$VAULT1" OPS_REMOTE="test-crypt:ops-vault" \
       OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MARKER="$MARK1" run_snap)
RC1=$?
check "first run exits 0" "$RC1" "0"
if [ "$RC1" -ne 0 ]; then
  echo "$OUT1"
  echo "SUMMARY: $pass passed, $((fail+1)) failed"
  exit 1
fi
home_after=$(snapshot_home "$FAKE_HOME" "$MEM_MARKER_EXCLUDE")

echo "== 2a. every newly allowlisted dir that exists lands; the missing one is skipped without error =="
[ -f "$VAULT1/claude/handoffs/note1.md" ]      && ok "handoffs landed"      || bad "handoffs missing"
[ -f "$VAULT1/claude/observability/o1.txt" ]   && ok "observability landed" || bad "observability missing"
[ -f "$VAULT1/claude/jobs/j1.txt" ]            && ok "jobs landed"          || bad "jobs missing"
[ -f "$VAULT1/claude/scheduled/s1.txt" ]       && ok "scheduled landed"     || bad "scheduled missing"
[ -f "$VAULT1/claude/watch/w1.txt" ]           && ok "watch landed"        || bad "watch missing"
[ -f "$VAULT1/claude/state/st1.txt" ]          && ok "state landed"        || bad "state missing"
[ ! -e "$VAULT1/claude/scheduled-tasks" ]      && ok "missing scheduled-tasks skipped, no error (rc already 0)" \
                                                || bad "scheduled-tasks should not exist"

echo "== 2b. ~/.local/bin: regular file copied, symlink not present as link or as target content =="
[ -f "$VAULT1/home/local-bin/real-script.sh" ] && ok "real-script.sh copied" || bad "real-script.sh missing"
[ ! -e "$VAULT1/home/local-bin/claude" ]       && ok "claude symlink not present at all" \
                                                || bad "claude symlink (or its dereferenced content) is present"
grep -rq "SYMLINK_TARGET_CANARY_TEXT" "$VAULT1" 2>/dev/null \
  && bad "symlink target content leaked into the vault" \
  || ok "symlink target content not present anywhere in the vault"

echo "== 2c. allowlist assertion (positive form): only known names present, locally and offsite =="
assert_allowlisted "$VAULT1/claude" "$CLAUDE_ALLOWED" "vault claude/ top level"
assert_allowlisted "$VAULT1/home"   "$HOME_ALLOWED"   "vault home/ top level"
[ -f "$VAULT1/home/.gitignore_global" ] && ok ".gitignore_global landed in the vault" || bad ".gitignore_global missing from the vault"
grep -rq "notreal" "$VAULT1" 2>/dev/null \
  && bad "secrets/apikey.txt content leaked into the vault" \
  || ok "secrets/apikey.txt content not present anywhere in the vault"
grep -rq "not-a-real-cert" "$VAULT1" 2>/dev/null \
  && bad ".cloudflared content leaked into the vault" \
  || ok ".cloudflared content not present anywhere in the vault"
grep -rq "not-a-real-env" "$VAULT1" 2>/dev/null \
  && bad ".env content leaked into the vault" \
  || ok ".env content not present anywhere in the vault"

echo "== h. the fixture home is byte-identical before and after the run =="
check "home tree unchanged by a snapshot run" "$home_after" "$home_before"

echo "== offsite: run 1 actually landed on the (fake) remote =="
LS1=$(rclone lsjson --recursive test-crypt:ops-vault/current 2>&1)
echo "$LS1" | grep -q '"Path":"claude/handoffs/note1.md"' \
  && ok "handoffs/note1.md present offsite" || bad "handoffs/note1.md missing offsite: $LS1"
MEMLS1=$(rclone lsjson --recursive test-crypt:claude-memory/current 2>&1)
echo "$MEMLS1" | grep -q '"Path":"mem1.bin"' \
  && ok "claude-memory/mem1.bin present offsite" || bad "claude-memory/mem1.bin missing offsite: $MEMLS1"
[ -s "$MARK1" ] && ok "OPS_MARKER written after a clean run" || bad "OPS_MARKER missing after a clean run"
assert_allowlisted_remote "test-crypt:ops-vault/current/claude" "$CLAUDE_ALLOWED" "offsite claude/ top level"
assert_allowlisted_remote "test-crypt:ops-vault/current/home"   "$HOME_ALLOWED"   "offsite home/ top level"

echo "== T12 (F4 + F5a/F5b pin): legacy count line has the right N; a clean run's output is quiet =="
# Computed right here, while run 1's push to test-crypt:ops-vault/current is still the
# latest state of that remote path -- later tests push MORE changes to the same remote,
# so a fresh 'rclone size' taken near the end of this suite would no longer match what
# $OUT1 actually reported at the time.
N1_REPORTED=$(echo "$OUT1" | sed -n 's/.*verified \([0-9][0-9]*\) objects offsite at.*/\1/p' | head -1)
N1_ACTUAL=$(rclone size test-crypt:ops-vault/current --json 2>/dev/null \
            | python3 -c "import json,sys; print(json.load(sys.stdin).get('count',''))" 2>/dev/null)
check "T12 (F4): legacy 'verified N objects offsite' line reports the actual remote object count" "$N1_REPORTED" "$N1_ACTUAL"
echo "$OUT1" | grep -q "No common hash found" \
  && bad "T12 (F5a): 'No common hash found' NOTICE leaked into a clean run's output" \
  || ok "T12 (F5a): no 'No common hash found' NOTICE in a clean run's output"
echo "$OUT1" | grep -q "skipping non-regular file" \
  && bad "T12 (F5b): 'skipping non-regular file' notice leaked into a clean run's output" \
  || ok "T12 (F5b): no 'skipping non-regular file' notice in a clean run's output"
echo "$OUT1" | grep -qx "" \
  && bad "T12 (F8): a clean run's output contains a blank line" \
  || ok "T12 (F8): a clean run's output has no blank lines"

echo "== T2 (F1 pin): a Finder .DS_Store dropped directly in the vault and in the claude-memory source =="
touch "$VAULT1/claude/handoffs/.DS_Store"
touch "$FAKE_HOME/data-vaults/claude-memory/.DS_Store"
OUTDS1=$(OPS_VAULT="$VAULT1" OPS_REMOTE="test-crypt:ops-vault" \
         OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MARKER="$MARK1" run_snap)
RCDS1=$?
check "REAL script (green): .DS_Store present in vault + claude-memory source still exits 0" "$RCDS1" "0"
LSDS1=$(rclone lsjson --recursive test-crypt:ops-vault/current 2>&1)
echo "$LSDS1" | grep -q '\.DS_Store' \
  && bad ".DS_Store leaked offsite in the ops-vault leg: $LSDS1" \
  || ok ".DS_Store never reached the ops-vault offsite copy"
MEMLSDS1=$(rclone lsjson --recursive test-crypt:claude-memory/current 2>&1)
echo "$MEMLSDS1" | grep -q '\.DS_Store' \
  && bad ".DS_Store leaked offsite in the claude-memory leg: $MEMLSDS1" \
  || ok ".DS_Store never reached the claude-memory offsite copy"

echo "== T2 continued: MUTATION-PROVE the check needs the same filter as its push (red), then restore (green) =="
MUTOLD_T2="$WORK/mut-t2-old.txt"; MUTNEW_T2="$WORK/mut-t2-new.txt"
printf '%s' 'check_out=$(rclone check --one-way --size-only "${MAIN_FILTER[@]}" "$VAULT" "$REMOTE/current" 2>&1)' > "$MUTOLD_T2"
printf '%s' 'check_out=$(rclone check --one-way --size-only "$VAULT" "$REMOTE/current" 2>&1)' > "$MUTNEW_T2"
if mutate_block "$MUTOLD_T2" "$MUTNEW_T2" "$SUBJECT"; then
  ok "mutation applied: main check no longer shares the push's .DS_Store filter"
  VAULTT2="$WORK/vault-t2-mutant"
  MARKT2="$WORK/vault-t2-mutant.marker.json"
  OPS_VAULT="$VAULTT2" OPS_REMOTE="test-crypt:ops-vault-t2" \
    OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 OPS_MARKER="$MARKT2" run_snap >/dev/null
  touch "$VAULTT2/claude/handoffs/.DS_Store"
  OUTT2M=$(OPS_VAULT="$VAULTT2" OPS_REMOTE="test-crypt:ops-vault-t2" \
    OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 OPS_MARKER="$MARKT2" run_snap)
  RCT2M=$?
  if [ "$RCT2M" -ne 0 ] && echo "$OUTT2M" | grep -qi "rclone check found differences"; then
    ok "MUTATION CONFIRMED (red): unfiltered check dies on the .DS_Store the push correctly skipped -- $(echo "$OUTT2M" | grep -i 'rclone check found differences' | head -1)"
  else
    bad "MUTATION did not reproduce the defect (rc=$RCT2M): $OUTT2M"
  fi
  restore_subject "T2 filter"
  OUTT2G=$(OPS_VAULT="$VAULTT2" OPS_REMOTE="test-crypt:ops-vault-t2" \
    OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 OPS_MARKER="$MARKT2" run_snap)
  RCT2G=$?
  check "restored script (green): same .DS_Store scenario exits 0" "$RCT2G" "0"
  echo "$OUTT2G" | tail -1
else
  bad "could not apply T2 mutation (block not found exactly once); script left untouched"
fi

echo "== e. per-file verify: an offsite object goes missing behind the script's back =="
rclone delete "test-crypt:ops-vault/current/claude/handoffs/note1.md" >/dev/null 2>&1
GONE=$(rclone lsjson --recursive test-crypt:ops-vault/current 2>&1)
echo "$GONE" | grep -q '"Path":"claude/handoffs/note1.md"' \
  && bad "setup error: file still present after deliberate delete" \
  || ok "setup: offsite copy of note1.md deliberately removed"
OUT2=$(OPS_VAULT="$VAULT1" OPS_REMOTE="test-crypt:ops-vault" \
       OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MARKER="$MARK1" run_snap)
RC2=$?
check "rerun after offsite drift exits 0" "$RC2" "0"
LS2=$(rclone lsjson --recursive test-crypt:ops-vault/current 2>&1)
echo "$LS2" | grep -q '"Path":"claude/handoffs/note1.md"' \
  && ok "note1.md is back offsite after rerun (re-uploaded)" \
  || bad "note1.md still missing offsite after rerun: $LS2"

echo "== f. claude-memory leg: copy semantics (local delete does not delete offsite; overwrite archives the old version) =="
rm -f "$FAKE_HOME/data-vaults/claude-memory/mem1.bin"
echo "memory snapshot 2" > "$FAKE_HOME/data-vaults/claude-memory/mem2.bin"
sleep 1
OUT3=$(OPS_VAULT="$VAULT1" OPS_REMOTE="test-crypt:ops-vault" \
       OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MARKER="$MARK1" run_snap)
RC3=$?
check "rerun after local memory delete+add exits 0" "$RC3" "0"
MEMLS3=$(rclone lsjson --recursive test-crypt:claude-memory/current 2>&1)
echo "$MEMLS3" | grep -q '"Path":"mem1.bin"' \
  && ok "mem1.bin (locally deleted) is STILL present offsite -- copy semantics, not sync" \
  || bad "mem1.bin was deleted offsite; claude-memory leg must never delete (rclone copy, not sync)"
echo "$MEMLS3" | grep -q '"Path":"mem2.bin"' \
  && ok "mem2.bin (new local file) landed offsite" || bad "mem2.bin missing offsite"

echo "memory snapshot 2 CHANGED" > "$FAKE_HOME/data-vaults/claude-memory/mem2.bin"
sleep 1
OUT4=$(OPS_VAULT="$VAULT1" OPS_REMOTE="test-crypt:ops-vault" \
       OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MARKER="$MARK1" run_snap)
RC4=$?
check "rerun after local memory overwrite exits 0" "$RC4" "0"
NEWCONTENT=$(rclone cat test-crypt:claude-memory/current/mem2.bin 2>/dev/null)
check "offsite mem2.bin now has the new content" "$NEWCONTENT" "memory snapshot 2 CHANGED"
REPLACED=$(rclone lsjson --recursive test-crypt:claude-memory/replaced 2>&1)
OLDPATH=$(echo "$REPLACED" | python3 -c "import sys,json; d=json.load(sys.stdin); print(next((e['Path'] for e in d if e['Path'].endswith('mem2.bin')), ''))" 2>/dev/null)
if [ -n "$OLDPATH" ]; then
  OLDCONTENT=$(rclone cat "test-crypt:claude-memory/replaced/$OLDPATH" 2>/dev/null)
  check "the overwritten mem2.bin's OLD content is archived under replaced/" "$OLDCONTENT" "memory snapshot 2"
else
  bad "no archived copy of the overwritten mem2.bin found under replaced/"
fi

echo "== T5a (F2 pin): a failed claude-memory leg leaves the ops marker of that run intact and writes no claude-memory marker =="
# F7 moved this leg's own marker to its own directory (claude-memory-offsite/,
# never inside claude-memory/ itself, and OPS_MEM_MARKER is unset here so this
# is the default path) -- pinning the old memory-sync-shaped path here would
# make this check vacuously pass (always "__absent__") regardless of whether a
# refused leg wrote a marker at the path it actually uses.
MEM_MARKER_PATH="$FAKE_HOME/data-vaults/claude-memory-offsite/last-ok.json"
MEM_MARKER_BEFORE=$(cat "$MEM_MARKER_PATH" 2>/dev/null || echo "__absent__")
VAULTF2="$WORK/vault-f2"
MARKF2="$WORK/vault-f2.marker.json"
OUTF2=$(OPS_VAULT="$VAULTF2" OPS_REMOTE="test-crypt:ops-vault-f2" \
       OPS_MEMORY_REMOTE="test-plain:$PLAINBACKING" OPS_MARKER="$MARKF2" run_snap)
RCF2=$?
check "ops leg ok, claude-memory leg refused (non-crypt): run exits 1" "$RCF2" "1"
echo "$OUTF2" | grep -qi "not crypt" && ok "T5a: refusal names the remote type" || bad "T5a: refusal message missing 'not crypt'"
[ -s "$MARKF2" ] && ok "T5a: this run's OWN ops marker WAS written (the ops leg itself succeeded first)" \
                  || bad "T5a: ops marker missing even though the ops leg should have succeeded first"
MEM_MARKER_AFTER=$(cat "$MEM_MARKER_PATH" 2>/dev/null || echo "__absent__")
check "T5a: claude-memory marker unchanged by a failed leg" "$MEM_MARKER_AFTER" "$MEM_MARKER_BEFORE"

echo "== T5b (F3 pin): an unchanged second run prints 'no changes' and makes no new commit in the vault =="
VAULTNC="$WORK/vault-nochange"
MARKNC="$WORK/vault-nochange.marker.json"
OUTNC1=$(OPS_VAULT="$VAULTNC" OPS_REMOTE="test-crypt:ops-vault-nc" \
        OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 OPS_MARKER="$MARKNC" run_snap)
RCNC1=$?
check "no-change fixture: first (bootstrap) run exits 0" "$RCNC1" "0"
HEADNC1=$(git -C "$VAULTNC" rev-parse HEAD 2>/dev/null)
OUTNC2=$(OPS_VAULT="$VAULTNC" OPS_REMOTE="test-crypt:ops-vault-nc" \
        OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 OPS_MARKER="$MARKNC" run_snap)
RCNC2=$?
check "no-change fixture: second (unchanged) run exits 0" "$RCNC2" "0"
HEADNC2=$(git -C "$VAULTNC" rev-parse HEAD 2>/dev/null)
check "unchanged second run makes no new vault commit" "$HEADNC2" "$HEADNC1"
echo "$OUTNC2" | grep -q "ops-snapshot: no changes at" \
  && ok "unchanged second run prints 'no changes'" || bad "unchanged second run did not print 'no changes': $OUTNC2"

echo "== T6 (M09 pin): missing-directory skips are silent, not failures =="
NODIR_HOME="$WORK/home-no-memdir"
mkdir -p "$NODIR_HOME/.claude/handoffs" "$NODIR_HOME/Library/LaunchAgents"
echo "note" > "$NODIR_HOME/.claude/handoffs/note.md"
# no data-vaults/claude-memory at all
OUTND=$(OPS_HOME="$NODIR_HOME" OPS_VAULT="$WORK/vault-no-memdir" OPS_REMOTE="test-crypt:ops-vault-nomem" \
        OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MARKER="$WORK/vault-no-memdir.marker.json" run_snap)
RCND=$?
check "fixture with NO data-vaults/claude-memory dir exits 0" "$RCND" "0"
echo "$OUTND" | grep -qi "claude-memory offsite skipped" \
  && ok "missing claude-memory dir: skip message printed" || bad "missing claude-memory dir: no skip message: $OUTND"

NOHANDOFF_HOME="$WORK/home-no-handoffs"
mkdir -p "$NOHANDOFF_HOME/data-vaults/claude-memory" "$NOHANDOFF_HOME/Library/LaunchAgents"
echo "mem" > "$NOHANDOFF_HOME/data-vaults/claude-memory/m.bin"
# no .claude/handoffs at all
OUTNH=$(OPS_HOME="$NOHANDOFF_HOME" OPS_VAULT="$WORK/vault-no-handoffs" OPS_REMOTE="test-crypt:ops-vault-nohandoff" \
        OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MARKER="$WORK/vault-no-handoffs.marker.json" run_snap)
RCNH=$?
check "fixture with NO .claude/handoffs dir exits 0" "$RCNH" "0"
[ ! -e "$WORK/vault-no-handoffs/claude/handoffs" ] \
  && ok "missing handoffs dir: nothing created for it in the vault" \
  || bad "missing handoffs dir: unexpectedly present in vault"

echo "== c. OPS_MEMORY_VAULT_OFFSITE=0 skips the claude-memory leg entirely =="
VAULTG="$WORK/vault-memoff"
MARKG="$WORK/vault-memoff.marker.json"
echo "memory snapshot only-local" > "$FAKE_HOME/data-vaults/claude-memory/mem3-localonly.bin"
OUT5=$(OPS_VAULT="$VAULTG" OPS_REMOTE="test-crypt:ops-vault" \
       OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 OPS_MARKER="$MARKG" run_snap)
RC5=$?
check "run with OPS_MEMORY_VAULT_OFFSITE=0 exits 0" "$RC5" "0"
echo "$OUT5" | grep -qi "claude-memory offsite disabled" \
  && ok "run announces the memory leg was disabled" || bad "no disabled-leg message printed"
MEMLS5=$(rclone lsjson --recursive test-crypt:claude-memory/current 2>&1)
echo "$MEMLS5" | grep -q "mem3-localonly.bin" \
  && bad "OPS_MEMORY_VAULT_OFFSITE=0 still pushed the new local file offsite" \
  || ok "mem3-localonly.bin correctly never reached the (disabled) offsite leg"

echo "== g. a configured-but-non-crypt remote is refused before anything uploads =="
VAULTP="$WORK/vault-plain"
MARKP="$WORK/vault-plain.marker.json"
OUT6=$(OPS_VAULT="$VAULTP" OPS_REMOTE="test-plain:$PLAINBACKING" \
       OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MARKER="$MARKP" run_snap)
RC6=$?
check "non-crypt OPS_REMOTE is refused" "$RC6" "1"
echo "$OUT6" | grep -qi "not crypt" && ok "refusal names the remote type" || bad "refusal message missing 'not crypt'"
[ -s "$MARKP" ] && bad "OPS_MARKER written despite refused offsite" || ok "no OPS_MARKER on refused offsite"
if [ -d "$PLAINBACKING" ]; then
  n=$(find "$PLAINBACKING" -type f | wc -l | tr -d ' ')
  check "nothing was written to the plaintext remote's backing dir" "$n" "0"
else
  ok "plaintext remote's backing dir was never even created"
fi

echo "== d. failed push: exit nonzero, marker not written (plus MUTATION-PROVE the swallowed-exit-status defect) =="
BADBACKING1="$WORK/badbacking1"; mkdir -p "$BADBACKING1"
PWB=$(rclone obscure bad-pw-1); PWB2=$(rclone obscure bad-pw-2)
cat >> "$RCONF" <<CFG

[bad-crypt-1]
type = crypt
remote = $BADBACKING1
password = $PWB
password2 = $PWB2
CFG
chmod -R 555 "$BADBACKING1"

MUTOLD="$WORK/mut-d-old.txt"; MUTNEW="$WORK/mut-d-new.txt"
cat > "$MUTOLD" <<'BLOCK'
sync_out=$(rclone sync "$VAULT" "$REMOTE/current" \
      --backup-dir "$REMOTE/replaced/$STAMP" \
      "${MAIN_FILTER[@]}" --transfers 4 --timeout 5m 2>&1)
sync_rc=$?
[ -n "$sync_out" ] && printf '%s\n' "$sync_out" | tail -3
[ "$sync_rc" -eq 0 ] || die "rclone sync to $REMOTE (rc=$sync_rc)"
BLOCK
cat > "$MUTNEW" <<'BLOCK'
if rclone sync "$VAULT" "$REMOTE/current" \
      --backup-dir "$REMOTE/replaced/$STAMP" \
      "${MAIN_FILTER[@]}" --transfers 4 --timeout 5m 2>&1 | tail -3; then
  :
else
  die "rclone sync to $REMOTE"
fi
BLOCK
if mutate_block "$MUTOLD" "$MUTNEW" "$SUBJECT"; then
  ok "mutation applied: reintroduced the swallowed-exit-status pipe bug"
  VAULTM="$WORK/vault-mutant-d"
  MARKM="$WORK/vault-mutant-d.marker.json"
  OUTM=$(OPS_VAULT="$VAULTM" OPS_REMOTE="bad-crypt-1:sub" \
         OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 OPS_MARKER="$MARKM" run_snap)
  RCM=$?
  # The script has TWO independent layers against a bad push: the sync
  # step's own rc capture, and a separate downstream `rclone check` that
  # runs regardless. Mutating only the first still lets the second layer
  # catch the resulting drift and fail the run overall (rc=1) -- so the
  # precise symptom of THIS defect is the sync step's own false claim of
  # success, not necessarily the script's final exit code.
  if [ "$RCM" -eq 0 ] && [ -s "$MARKM" ]; then
    ok "MUTATION CONFIRMED (full false-positive): rc=0 and OPS_MARKER written despite a failed push"
  elif echo "$OUTM" | grep -q "ops-snapshot: pushed to"; then
    ok "MUTATION CONFIRMED: the sync step itself falsely claims \"pushed to ...\" despite rclone reporting errors underneath -- exactly the historical swallowed-exit-status defect. (The run still ends nonzero here only because the separate, unmutated 'rclone check' step independently catches the resulting drift -- a second layer, not a substitute for capturing sync's own exit status.)"
  else
    bad "mutation did not reproduce the swallowed-exit-status defect at all (rc=$RCM) -- fixture or mutation is wrong"
  fi
  restore_subject "case d"
else
  bad "could not apply case d mutation (block not found exactly once); script left untouched"
fi

BADBACKING2="$WORK/badbacking2"; mkdir -p "$BADBACKING2"
chmod -R 555 "$BADBACKING2"
cat >> "$RCONF" <<CFG

[bad-crypt-2]
type = crypt
remote = $BADBACKING2
password = $PWB
password2 = $PWB2
CFG
VAULTR="$WORK/vault-real-d"
MARKR="$WORK/vault-real-d.marker.json"
OUTR=$(OPS_VAULT="$VAULTR" OPS_REMOTE="bad-crypt-2:sub" \
       OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 OPS_MARKER="$MARKR" run_snap)
RCR=$?
check "REAL (restored) script: failed push exits nonzero" "$RCR" "1"
[ -s "$MARKR" ] && bad "REAL script wrote OPS_MARKER despite a failed push" \
                 || ok "REAL script correctly wrote no OPS_MARKER on a failed push"
echo "$OUTR" | grep -q "ops-snapshot: pushed to" \
  && bad "REAL script printed 'pushed to' despite a failed push" \
  || ok "REAL script correctly printed no 'pushed to' line on a failed push"
chmod -R 755 "$BADBACKING1" "$BADBACKING2" 2>/dev/null

echo "== b. MUTATION-PROVE: .local/bin symlink handling =="
MUTOLD_B="$WORK/mut-b-old.txt"; MUTNEW_B="$WORK/mut-b-new.txt"
printf '%s' 'rsync -a --no-links --delete --exclude '"'"'.DS_Store'"'"' \' > "$MUTOLD_B"
printf '%s' 'rsync -aL --delete --exclude '"'"'.DS_Store'"'"' \' > "$MUTNEW_B"
if mutate_block "$MUTOLD_B" "$MUTNEW_B" "$SUBJECT"; then
  ok "mutation applied: .local/bin rsync now follows symlinks (-L instead of --no-links)"
  VAULTB="$WORK/vault-mutant-b"
  MARKB="$WORK/vault-mutant-b.marker.json"
  OUTB=$(OPS_VAULT="$VAULTB" OPS_REMOTE="test-crypt:ops-vault-b" \
         OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 OPS_MARKER="$MARKB" run_snap)
  if grep -rq "SYMLINK_TARGET_CANARY_TEXT" "$VAULTB" 2>/dev/null; then
    ok "MUTATION CONFIRMED: following the symlink leaks its target content into the vault -- this test would have caught it"
  else
    bad "mutation did not reproduce the symlink-following defect -- fixture or mutation is wrong"
  fi
  restore_subject "case b"
else
  bad "could not apply case b mutation (block not found exactly once); script left untouched"
fi

echo "== c. MUTATION-PROVE: an unlisted directory sneaking into the vault, caught by the allowlist check itself =="
MUTOLD_C="$WORK/mut-c-old.txt"; MUTNEW_C="$WORK/mut-c-new.txt"
printf '%s' 'sync_dir "$OPS_HOME/.claude/handoffs/"        "claude/handoffs/"' > "$MUTOLD_C"
{ printf '%s\n' 'sync_dir "$OPS_HOME/.claude/handoffs/"        "claude/handoffs/"'
  printf '%s' 'sync_dir "$OPS_HOME/.claude/secrets/" "claude/secrets/"'; } > "$MUTNEW_C"
if mutate_block "$MUTOLD_C" "$MUTNEW_C" "$SUBJECT"; then
  ok "mutation applied: secrets/ added to the sync allowlist"
  VAULTC="$WORK/vault-mutant-c"
  MARKC="$WORK/vault-mutant-c.marker.json"
  OUTC=$(OPS_VAULT="$VAULTC" OPS_REMOTE="test-crypt:ops-vault-c" \
         OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 OPS_MARKER="$MARKC" run_snap)
  BADNAMES_C=$(allowlist_bad_names "$VAULTC/claude" "$CLAUDE_ALLOWED")
  if printf '%s' "$BADNAMES_C" | grep -qw "secrets"; then
    ok "MUTATION CONFIRMED: the allowlist check itself (not just a direct path test) flags 'secrets' as unexpected under vault claude/"
  else
    bad "MUTATION did not reproduce the leak via the allowlist check (bad_names='$BADNAMES_C')"
  fi
  [ -e "$VAULTC/claude/secrets" ] && ok "secrets/ directly present under vault claude/ (corroborating check)" \
                                   || bad "secrets/ unexpectedly absent (fixture or mutation is wrong)"
  restore_subject "case c"
else
  bad "could not apply case c mutation (block not found exactly once); script left untouched"
fi

echo "== T3 (M01 pin): second-resolution STAMP keeps distinct archives for two same-day overwrites =="
# Content strings are deliberately different LENGTHS (not just different text):
# same-length overwrites can round-trip through rclone's size-based change
# detection without ever being recognised as a diff, which would make this
# scenario prove nothing about STAMP at all.
STAMP_V_A="stamp-test-version-A"
STAMP_V_B="stamp-test-version-B-is-longer"
STAMP_V_C="stamp-test-version-C-is-longer-still"
run_stamp_scenario() {  # $1 home dir  $2 vault dir  $3 remote name  $4 marker path
  mkdir -p "$1/.claude/handoffs" "$1/Library/LaunchAgents"
  echo "$STAMP_V_A" > "$1/.claude/handoffs/f.md"
  OPS_HOME="$1" OPS_VAULT="$2" OPS_REMOTE="test-crypt:$3" \
    OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 OPS_MARKER="$4" run_snap >/dev/null
  echo "$STAMP_V_B" > "$1/.claude/handoffs/f.md"
  sleep 1
  OPS_HOME="$1" OPS_VAULT="$2" OPS_REMOTE="test-crypt:$3" \
    OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 OPS_MARKER="$4" run_snap >/dev/null
  sleep 1
  echo "$STAMP_V_C" > "$1/.claude/handoffs/f.md"
  OPS_HOME="$1" OPS_VAULT="$2" OPS_REMOTE="test-crypt:$3" \
    OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 OPS_MARKER="$4" run_snap >/dev/null
}
archives_have_both_versions() {  # $1 remote-name -> prints "1" "1" for found-A found-B (space separated)
  local remote="$1" p c fa=0 fb=0
  for p in $(list_archived_paths "test-crypt:$remote/replaced" "f.md"); do
    c=$(rclone cat "test-crypt:$remote/replaced/$p" 2>/dev/null)
    [ "$c" = "$STAMP_V_A" ] && fa=1
    [ "$c" = "$STAMP_V_B" ] && fb=1
  done
  echo "$fa $fb"
}
run_stamp_scenario "$WORK/home-stamp" "$WORK/vault-stamp" "ops-vault-stamp" "$WORK/vault-stamp.marker.json"
read -r FA FB <<EOF
$(archives_have_both_versions "ops-vault-stamp")
EOF
if [ "$FA" = "1" ] && [ "$FB" = "1" ]; then
  ok "REAL script: both same-day overwrites (version A and version B) are archived distinctly under replaced/"
else
  bad "REAL script: expected both 'version A' and 'version B' archived distinctly under replaced/ (found A=$FA B=$FB)"
fi

MUTOLD_M01="$WORK/mut-m01-old.txt"; MUTNEW_M01="$WORK/mut-m01-new.txt"
printf '%s' "STAMP=\$(date '+%Y-%m-%dT%H%M%S')" > "$MUTOLD_M01"
printf '%s' "STAMP=\$(date '+%Y-%m-%d')" > "$MUTNEW_M01"
if mutate_block "$MUTOLD_M01" "$MUTNEW_M01" "$SUBJECT"; then
  ok "mutation applied: STAMP reverted to date-only resolution (M01)"
  run_stamp_scenario "$WORK/home-stamp-mut" "$WORK/vault-stamp-mut" "ops-vault-stamp-mut" "$WORK/vault-stamp-mut.marker.json"
  read -r MFA MFB <<EOF
$(archives_have_both_versions "ops-vault-stamp-mut")
EOF
  if [ "$MFA" = "1" ] && [ "$MFB" = "1" ]; then
    bad "MUTATION not caught: date-only STAMP still kept both same-day overwrites distinct (fixture or mutation wrong)"
  else
    ok "MUTATION CONFIRMED (M01): date-only STAMP collides same-day backup-dirs, losing an intermediate archived version (found A=$MFA B=$MFB)"
  fi
  restore_subject "M01 (STAMP)"
else
  bad "could not apply M01 mutation (block not found exactly once); script left untouched"
fi

echo "== T3 (dry-run trick, ops leg): RCLONE_DRY_RUN=true masks a non-upload; the per-file check must still catch it =="
DRY_HOME="$WORK/home-dryrun"
mkdir -p "$DRY_HOME/.claude/handoffs" "$DRY_HOME/Library/LaunchAgents"
echo "dry version A" > "$DRY_HOME/.claude/handoffs/f.md"
VAULTDRY="$WORK/vault-dryrun"
MARKDRY="$WORK/vault-dryrun.marker.json"
OPS_HOME="$DRY_HOME" OPS_VAULT="$VAULTDRY" OPS_REMOTE="test-crypt:ops-vault-dry" \
  OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 OPS_MARKER="$MARKDRY" run_snap >/dev/null
MARKDRY_BEFORE=$(cat "$MARKDRY" 2>/dev/null)
echo "dry version B (never actually uploaded under dry-run)" > "$DRY_HOME/.claude/handoffs/f.md"
OUTDRY=$(OPS_HOME="$DRY_HOME" OPS_VAULT="$VAULTDRY" OPS_REMOTE="test-crypt:ops-vault-dry" \
  OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 OPS_MARKER="$MARKDRY" RCLONE_DRY_RUN=true run_snap)
RCDRY=$?
check "REAL script (green): dry-run-masked drift makes the run exit nonzero" "$RCDRY" "1"
MARKDRY_AFTER=$(cat "$MARKDRY" 2>/dev/null)
check "REAL script: marker unchanged by a dry-run-masked non-upload (the per-file check caught the drift)" "$MARKDRY_AFTER" "$MARKDRY_BEFORE"

echo "== T3 (M02 pin): MUTATION-PROVE main per-file check result forced to 0 =="
MUTOLD_M02="$WORK/mut-m02-old.txt"; MUTNEW_M02="$WORK/mut-m02-new.txt"
cat > "$MUTOLD_M02" <<'BLOCK'
check_out=$(rclone check --one-way --size-only "${MAIN_FILTER[@]}" "$VAULT" "$REMOTE/current" 2>&1)
check_rc=$?
BLOCK
cat > "$MUTNEW_M02" <<'BLOCK'
check_out=$(rclone check --one-way --size-only "${MAIN_FILTER[@]}" "$VAULT" "$REMOTE/current" 2>&1)
check_rc=0
BLOCK
if mutate_block "$MUTOLD_M02" "$MUTNEW_M02" "$SUBJECT"; then
  ok "mutation applied: main per-file rclone check result forced to 0 (M02)"
  DRY_HOME2="$WORK/home-dryrun-mut"
  mkdir -p "$DRY_HOME2/.claude/handoffs" "$DRY_HOME2/Library/LaunchAgents"
  echo "dry version A" > "$DRY_HOME2/.claude/handoffs/f.md"
  VAULTDRYM="$WORK/vault-dryrun-mut"
  MARKDRYM="$WORK/vault-dryrun-mut.marker.json"
  OPS_HOME="$DRY_HOME2" OPS_VAULT="$VAULTDRYM" OPS_REMOTE="test-crypt:ops-vault-dry-mut" \
    OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 OPS_MARKER="$MARKDRYM" run_snap >/dev/null
  echo "dry version B (never actually uploaded under dry-run)" > "$DRY_HOME2/.claude/handoffs/f.md"
  OUTDRYM=$(OPS_HOME="$DRY_HOME2" OPS_VAULT="$VAULTDRYM" OPS_REMOTE="test-crypt:ops-vault-dry-mut" \
    OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 OPS_MARKER="$MARKDRYM" RCLONE_DRY_RUN=true run_snap)
  RCDRYM=$?
  if [ "$RCDRYM" -eq 0 ] && [ -s "$MARKDRYM" ]; then
    ok "MUTATION CONFIRMED (M02): with the check gate defeated, a dry-run-masked non-upload falsely succeeds and writes the marker"
  else
    bad "MUTATION did not reproduce the false-success defect (rc=$RCDRYM)"
  fi
  restore_subject "M02"
else
  bad "could not apply M02 mutation (block not found exactly once); script left untouched"
fi

echo "== T3 (M06 pin): MUTATION-PROVE verify failure no longer gates the marker =="
MUTOLD_M06="$WORK/mut-m06-old.txt"; MUTNEW_M06="$WORK/mut-m06-new.txt"
printf '%s' '[ "$check_rc" -eq 0 ] || die "rclone check found differences between $VAULT and $REMOTE/current (rc=$check_rc)"' > "$MUTOLD_M06"
printf '%s' '[ "$check_rc" -eq 0 ] || true' > "$MUTNEW_M06"
if mutate_block "$MUTOLD_M06" "$MUTNEW_M06" "$SUBJECT"; then
  ok "mutation applied: main verify failure no longer gates the marker (M06)"
  DRY_HOME3="$WORK/home-dryrun-m06"
  mkdir -p "$DRY_HOME3/.claude/handoffs" "$DRY_HOME3/Library/LaunchAgents"
  echo "dry version A" > "$DRY_HOME3/.claude/handoffs/f.md"
  VAULTM06="$WORK/vault-mutant-m06"
  MARKM06="$WORK/vault-mutant-m06.marker.json"
  OPS_HOME="$DRY_HOME3" OPS_VAULT="$VAULTM06" OPS_REMOTE="test-crypt:ops-vault-m06" \
    OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 OPS_MARKER="$MARKM06" run_snap >/dev/null
  echo "dry version B (never actually uploaded under dry-run)" > "$DRY_HOME3/.claude/handoffs/f.md"
  OUTM06=$(OPS_HOME="$DRY_HOME3" OPS_VAULT="$VAULTM06" OPS_REMOTE="test-crypt:ops-vault-m06" \
    OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 OPS_MARKER="$MARKM06" RCLONE_DRY_RUN=true run_snap)
  RCM06=$?
  if [ "$RCM06" -eq 0 ] && [ -s "$MARKM06" ]; then
    ok "MUTATION CONFIRMED (M06): a real check failure no longer stops the marker from being written"
  else
    bad "MUTATION did not reproduce the defect (rc=$RCM06)"
  fi
  restore_subject "M06"
else
  bad "could not apply M06 mutation (block not found exactly once); script left untouched"
fi

echo "== T3 (dry-run trick, claude-memory leg) + M05 pin: same drift-masking, and a swallowed copy exit status =="
DRYM_HOME="$WORK/home-dryrun-mem"
mkdir -p "$DRYM_HOME/data-vaults/claude-memory" "$DRYM_HOME/Library/LaunchAgents"
echo "dry mem version A" > "$DRYM_HOME/data-vaults/claude-memory/m.bin"
VAULTDRYMEM="$WORK/vault-dryrun-mem"
MARKDRYMEM="$WORK/vault-dryrun-mem.marker.json"
MEMMARKDRYMEM="$WORK/vault-dryrun-mem.memmarker.json"
OPS_HOME="$DRYM_HOME" OPS_VAULT="$VAULTDRYMEM" OPS_REMOTE="test-crypt:ops-vault-dry-mem" \
  OPS_MEMORY_REMOTE="test-crypt:claude-memory-dry" OPS_MARKER="$MARKDRYMEM" OPS_MEM_MARKER="$MEMMARKDRYMEM" run_snap >/dev/null
# A NEW file, never an edit to an existing one: hand-verified that RCLONE_DRY_RUN=true can let
# an `rclone copy` of a CHANGED EXISTING file transfer for real (a quirk this suite's earlier
# form hit by accident, via F7's now-removed in-tree marker rewrite). A brand-new, never-before-
# pushed file gives a clean local-only diff regardless of that quirk -- confirmed by hand: the
# dry-run copy transfers nothing for a new file, and the per-file check below still catches it.
echo "dry mem version B (a NEW file, never actually uploaded under dry-run)" > "$DRYM_HOME/data-vaults/claude-memory/m2.bin"
MEMMARK_BEFORE=$(cat "$MEMMARKDRYMEM" 2>/dev/null)
OUTDRYMEM=$(OPS_HOME="$DRYM_HOME" OPS_VAULT="$VAULTDRYMEM" OPS_REMOTE="test-crypt:ops-vault-dry-mem" \
  OPS_MEMORY_REMOTE="test-crypt:claude-memory-dry" OPS_MARKER="$MARKDRYMEM" OPS_MEM_MARKER="$MEMMARKDRYMEM" RCLONE_DRY_RUN=true run_snap)
RCDRYMEM=$?
check "REAL script: claude-memory dry-run-masked drift (new file) makes the run exit nonzero" "$RCDRYMEM" "1"
MEMMARK_AFTER=$(cat "$MEMMARKDRYMEM" 2>/dev/null)
check "REAL script: claude-memory marker unchanged by a dry-run-masked drift" "$MEMMARK_AFTER" "$MEMMARK_BEFORE"

BADBACKINGM="$WORK/badbacking-mem"; mkdir -p "$BADBACKINGM"
PWM=$(rclone obscure bad-mem-pw-1); PWM2=$(rclone obscure bad-mem-pw-2)
cat >> "$RCONF" <<CFG

[bad-crypt-mem]
type = crypt
remote = $BADBACKINGM
password = $PWM
password2 = $PWM2
CFG
chmod -R 555 "$BADBACKINGM"
MUTOLD_M05="$WORK/mut-m05-old.txt"; MUTNEW_M05="$WORK/mut-m05-new.txt"
cat > "$MUTOLD_M05" <<'BLOCK'
          "${MEM_FILTER[@]}" --transfers 4 --timeout 30m 2>&1)
    mcopy_rc=$?
BLOCK
cat > "$MUTNEW_M05" <<'BLOCK'
          "${MEM_FILTER[@]}" --transfers 4 --timeout 30m 2>&1)
    mcopy_rc=0
BLOCK
if mutate_block "$MUTOLD_M05" "$MUTNEW_M05" "$SUBJECT"; then
  ok "mutation applied: claude-memory copy exit status forced to 0 (M05)"
  VAULTM5="$WORK/vault-mutant-m05"
  MARKM5="$WORK/vault-mutant-m05.marker.json"
  OUTM5=$(OPS_VAULT="$VAULTM5" OPS_REMOTE="test-crypt:ops-vault-m05" \
    OPS_MEMORY_REMOTE="bad-crypt-mem:sub" OPS_MARKER="$MARKM5" run_snap)
  RCM5=$?
  if [ "$RCM5" -eq 0 ] || echo "$OUTM5" | grep -q "ops-snapshot: pushed claude-memory to"; then
    ok "MUTATION CONFIRMED (M05): a genuinely failed claude-memory copy is reported as pushed once its exit status is swallowed"
  else
    bad "MUTATION did not reproduce the swallowed-exit-status defect for claude-memory (rc=$RCM5)"
  fi
  restore_subject "M05"
else
  bad "could not apply M05 mutation (block not found exactly once); script left untouched"
fi
chmod -R 755 "$BADBACKINGM" 2>/dev/null

echo "== T3 (M04 pin): MUTATION-PROVE claude-memory crypt-type check removed =="
MUTOLD_M04="$WORK/mut-m04-old.txt"; MUTNEW_M04="$WORK/mut-m04-new.txt"
cat > "$MUTOLD_M04" <<'BLOCK'
    mrtype=$(rclone config show "${MEMORY_REMOTE%%:*}" 2>/dev/null | sed -n 's/^type = //p')
    [ "$mrtype" = "crypt" ] || die "refusing claude-memory offsite: remote ${MEMORY_REMOTE%%:*} is type '${mrtype:-unknown}', not crypt" "claude-memory"
BLOCK
cat > "$MUTNEW_M04" <<'BLOCK'
    mrtype=$(rclone config show "${MEMORY_REMOTE%%:*}" 2>/dev/null | sed -n 's/^type = //p')
    : "crypt check disabled for mutation test"
BLOCK
if mutate_block "$MUTOLD_M04" "$MUTNEW_M04" "$SUBJECT"; then
  ok "mutation applied: claude-memory crypt-type refusal removed (M04)"
  PLAINBACKING2="$WORK/plainbacking-mem"; mkdir -p "$PLAINBACKING2"
  VAULTM04="$WORK/vault-mutant-m04"
  MARKM04="$WORK/vault-mutant-m04.marker.json"
  OUTM04=$(OPS_VAULT="$VAULTM04" OPS_REMOTE="test-crypt:ops-vault-m04" \
    OPS_MEMORY_REMOTE="test-plain:$PLAINBACKING2" OPS_MARKER="$MARKM04" run_snap)
  n=$(find "$PLAINBACKING2" -type f 2>/dev/null | wc -l | tr -d ' ')
  if [ "$n" -gt 0 ]; then
    ok "MUTATION CONFIRMED (M04): with the crypt check removed, claude-memory content was pushed PLAINTEXT to a non-crypt remote (n=$n file(s))"
  else
    bad "MUTATION did not reproduce the defect -- nothing landed in the plaintext backing dir (n=$n)"
  fi
  restore_subject "M04"
else
  bad "could not apply M04 mutation (block not found exactly once); script left untouched"
fi

echo "== T3 (M07 pin): MUTATION-PROVE main preflight removed =="
BADBACKINGPF="$WORK/badbacking-pf"; mkdir -p "$BADBACKINGPF"
PWPF=$(rclone obscure bad-pf-pw-1); PWPF2=$(rclone obscure bad-pf-pw-2)
cat >> "$RCONF" <<CFG

[bad-crypt-pf]
type = crypt
remote = $BADBACKINGPF
password = $PWPF
password2 = $PWPF2
CFG
chmod 000 "$BADBACKINGPF"
MUTOLD_M07="$WORK/mut-m07-old.txt"; MUTNEW_M07="$WORK/mut-m07-new.txt"
cat > "$MUTOLD_M07" <<'BLOCK'
preflight_err=$(rclone lsd --max-depth 1 "$REMOTE" --timeout 20s --contimeout 10s 2>&1 >/dev/null)
preflight_rc=$?
if [ "$preflight_rc" -ne 0 ] && [ "$preflight_rc" -ne 3 ]; then
  die "offsite preflight failed for $REMOTE (auth or reachability); rclone said: $preflight_err"
fi
BLOCK
cat > "$MUTNEW_M07" <<'BLOCK'
: "main preflight disabled for mutation test"
BLOCK
VAULTPF="$WORK/vault-preflight"
MARKPF="$WORK/vault-preflight.marker.json"
OUTPF=$(OPS_VAULT="$VAULTPF" OPS_REMOTE="bad-crypt-pf:" \
  OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 OPS_MARKER="$MARKPF" run_snap)
RCPF=$?
check "REAL script: unreachable OPS_REMOTE exits nonzero" "$RCPF" "1"
echo "$OUTPF" | grep -q "offsite preflight failed for" \
  && ok "REAL script: preflight names the failure distinctly, before any sync attempt" \
  || bad "REAL script: expected an 'offsite preflight failed for' message: $OUTPF"
if mutate_block "$MUTOLD_M07" "$MUTNEW_M07" "$SUBJECT"; then
  ok "mutation applied: main preflight removed (M07)"
  VAULTM07="$WORK/vault-mutant-m07"
  MARKM07="$WORK/vault-mutant-m07.marker.json"
  OUTM07=$(OPS_VAULT="$VAULTM07" OPS_REMOTE="bad-crypt-pf:" \
    OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 OPS_MARKER="$MARKM07" run_snap)
  if echo "$OUTM07" | grep -q "offsite preflight failed for"; then
    bad "MUTATION did not reproduce the defect -- preflight message still appeared with preflight removed"
  else
    ok "MUTATION CONFIRMED (M07): with preflight removed, the same unreachable remote fails later/differently with no distinct preflight message"
  fi
  restore_subject "M07"
else
  bad "could not apply M07 mutation (block not found exactly once); script left untouched"
fi

echo "== T3 (M14 pin): MUTATION-PROVE claude-memory preflight removed =="
BADBACKINGPFM="$WORK/badbacking-pf-mem"; mkdir -p "$BADBACKINGPFM"
PWPFM=$(rclone obscure bad-pfm-pw-1); PWPFM2=$(rclone obscure bad-pfm-pw-2)
cat >> "$RCONF" <<CFG

[bad-crypt-pf-mem]
type = crypt
remote = $BADBACKINGPFM
password = $PWPFM
password2 = $PWPFM2
CFG
chmod 000 "$BADBACKINGPFM"
MUTOLD_M14="$WORK/mut-m14-old.txt"; MUTNEW_M14="$WORK/mut-m14-new.txt"
cat > "$MUTOLD_M14" <<'BLOCK'
    mpre_err=$(rclone lsd --max-depth 1 "$MEMORY_REMOTE" --timeout 20s --contimeout 10s 2>&1 >/dev/null)
    mpre_rc=$?
    if [ "$mpre_rc" -ne 0 ] && [ "$mpre_rc" -ne 3 ]; then
      die "claude-memory offsite preflight failed for $MEMORY_REMOTE (auth or reachability); rclone said: $mpre_err" "claude-memory"
    fi
BLOCK
cat > "$MUTNEW_M14" <<'BLOCK'
    : "claude-memory preflight disabled for mutation test"
BLOCK
VAULTPFM="$WORK/vault-preflight-mem"
MARKPFM="$WORK/vault-preflight-mem.marker.json"
OUTPFM=$(OPS_VAULT="$VAULTPFM" OPS_REMOTE="test-crypt:ops-vault-pfm" \
  OPS_MEMORY_REMOTE="bad-crypt-pf-mem:" OPS_MARKER="$MARKPFM" run_snap)
RCPFM=$?
check "REAL script: unreachable OPS_MEMORY_REMOTE exits nonzero" "$RCPFM" "1"
echo "$OUTPFM" | grep -q "claude-memory offsite preflight failed for" \
  && ok "REAL script: claude-memory preflight names the failure distinctly" \
  || bad "REAL script: expected a 'claude-memory offsite preflight failed for' message: $OUTPFM"
if mutate_block "$MUTOLD_M14" "$MUTNEW_M14" "$SUBJECT"; then
  ok "mutation applied: claude-memory preflight removed (M14)"
  VAULTM14="$WORK/vault-mutant-m14"
  MARKM14="$WORK/vault-mutant-m14.marker.json"
  OUTM14=$(OPS_VAULT="$VAULTM14" OPS_REMOTE="test-crypt:ops-vault-m14" \
    OPS_MEMORY_REMOTE="bad-crypt-pf-mem:" OPS_MARKER="$MARKM14" run_snap)
  if echo "$OUTM14" | grep -q "claude-memory offsite preflight failed for"; then
    bad "MUTATION did not reproduce the defect -- preflight message still appeared with preflight removed"
  else
    ok "MUTATION CONFIRMED (M14): with claude-memory preflight removed, the same unreachable remote fails later/differently with no distinct preflight message"
  fi
  restore_subject "M14"
else
  bad "could not apply M14 mutation (block not found exactly once); script left untouched"
fi

echo "== T8 (P1 behaviour): claude-memory copy exit status gates everything downstream of it =="
BADBACKINGT8="$WORK/badbacking-t8"; mkdir -p "$BADBACKINGT8"
chmod -R 555 "$BADBACKINGT8"
PWT8=$(rclone obscure t8-pw-1); PWT8_2=$(rclone obscure t8-pw-2)
cat >> "$RCONF" <<CFG

[bad-crypt-t8]
type = crypt
remote = $BADBACKINGT8
password = $PWT8
password2 = $PWT8_2
CFG
VAULTT8="$WORK/vault-t8"
MARKT8="$WORK/vault-t8.marker.json"
MEMMARKT8="$WORK/vault-t8.memmarker.json"
OUTT8=$(OPS_VAULT="$VAULTT8" OPS_REMOTE="test-crypt:ops-vault-t8" \
  OPS_MEMORY_REMOTE="bad-crypt-t8:sub" OPS_MARKER="$MARKT8" OPS_MEM_MARKER="$MEMMARKT8" run_snap)
RCT8=$?
check "T8: REAL script -- failed claude-memory copy (unwritable backing for a new file) exits the whole run nonzero" "$RCT8" "1"
[ -s "$MARKT8" ] && ok "T8: ops-vault leg still succeeded and wrote its own marker before the memory leg failed" \
                  || bad "T8: ops marker missing even though the ops leg should have succeeded first"
[ -s "$MEMMARKT8" ] && bad "T8: claude-memory marker WAS written despite a failed copy" \
                      || ok "T8: no claude-memory marker written after a failed copy"
echo "$OUTT8" | grep -q "ops-snapshot: pushed claude-memory to" \
  && bad "T8: run printed 'pushed claude-memory to' despite a failed copy" \
  || ok "T8: run correctly printed no 'pushed claude-memory to' line"
chmod -R 755 "$BADBACKINGT8" 2>/dev/null

echo "== T9 (F2a/F2b pin): a failing leg files exactly one board task, keyed and titled per lane =="
FT_HOME="$WORK/home-failtask"
mkdir -p "$FT_HOME/.claude/bin" "$FT_HOME/data-vaults/claude-memory" "$FT_HOME/Library/LaunchAgents"
echo "mem" > "$FT_HOME/data-vaults/claude-memory/m.bin"
FT_LOG="$WORK/failtask.log"
cat > "$FT_HOME/.claude/bin/failtask" <<STUB
#!/bin/bash
printf '%s\n' "FAILTASK \$*" >> "$FT_LOG"
exit 0
STUB
chmod +x "$FT_HOME/.claude/bin/failtask"

rm -f "$FT_LOG"
VAULTT9A="$WORK/vault-t9a"
MARKT9A="$WORK/vault-t9a.marker.json"
HOME="$FT_HOME" OPS_HOME="$FT_HOME" RCLONE_CONFIG="$RCONF" \
  OPS_VAULT="$VAULTT9A" OPS_REMOTE="test-crypt:ops-vault-t9a" \
  OPS_MEMORY_REMOTE="test-plain:$PLAINBACKING" OPS_MARKER="$MARKT9A" \
  /bin/bash "$SUBJECT" >/dev/null 2>&1
FTCOUNT_A=$(grep -c "^FAILTASK " "$FT_LOG" 2>/dev/null); FTCOUNT_A="${FTCOUNT_A:-0}"
check "T9: failing claude-memory leg files exactly one failtask record" "$FTCOUNT_A" "1"
grep -q -- "--dedupe-key claude-memory-offsite-failed" "$FT_LOG" 2>/dev/null \
  && ok "T9: claude-memory failure uses its own dedupe key" \
  || bad "T9: claude-memory-offsite-failed dedupe key missing: $(cat "$FT_LOG" 2>/dev/null)"
grep -q "claude-memory offsite failed" "$FT_LOG" 2>/dev/null \
  && ok "T9: claude-memory failure title names claude-memory" \
  || bad "T9: title does not name claude-memory: $(cat "$FT_LOG" 2>/dev/null)"

rm -f "$FT_LOG"
VAULTT9B="$WORK/vault-t9b"
MARKT9B="$WORK/vault-t9b.marker.json"
HOME="$FT_HOME" OPS_HOME="$FT_HOME" RCLONE_CONFIG="$RCONF" \
  OPS_VAULT="$VAULTT9B" OPS_REMOTE="test-plain:$PLAINBACKING" \
  OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MARKER="$MARKT9B" \
  /bin/bash "$SUBJECT" >/dev/null 2>&1
FTCOUNT_B=$(grep -c "^FAILTASK " "$FT_LOG" 2>/dev/null); FTCOUNT_B="${FTCOUNT_B:-0}"
check "T9: failing ops-vault leg files exactly one failtask record" "$FTCOUNT_B" "1"
grep -q -- "--dedupe-key ops-snapshot-failed" "$FT_LOG" 2>/dev/null \
  && ok "T9: ops-vault failure uses its own dedupe key" \
  || bad "T9: ops-snapshot-failed dedupe key missing: $(cat "$FT_LOG" 2>/dev/null)"
grep -q "ops-snapshot failed" "$FT_LOG" 2>/dev/null \
  && ok "T9: ops-vault failure title names ops-snapshot" \
  || bad "T9: title does not name ops-snapshot: $(cat "$FT_LOG" 2>/dev/null)"

echo "== T10 (F2e + F7 pin): claude-memory marker content, and memory-sync's own last-ok.json is left untouched =="
T10_HOME="$WORK/home-t10"
mkdir -p "$T10_HOME/.claude/handoffs" "$T10_HOME/data-vaults/claude-memory" "$T10_HOME/Library/LaunchAgents"
echo "note" > "$T10_HOME/.claude/handoffs/n.md"
echo "t10 memory content" > "$T10_HOME/data-vaults/claude-memory/mem-t10.bin"
# Pre-seed the exact schema memory-sync itself writes (local-tools/memory-sync:605:
# {"snapshot":..., "ts":...}) at the top level of the source -- F7 requires this file be
# treated as ordinary data to back up, never read or written as this leg's own marker.
MEMSYNC_MARKER="$T10_HOME/data-vaults/claude-memory/last-ok.json"
printf '{"snapshot":"pre-existing","ts":"2026-09-28T01:00:00"}' > "$MEMSYNC_MARKER"
MEMSYNC_MARKER_BEFORE=$(cat "$MEMSYNC_MARKER")
VAULTT10="$WORK/vault-t10"
MARKT10="$WORK/vault-t10.marker.json"
OUTT10=$(OPS_HOME="$T10_HOME" OPS_VAULT="$VAULTT10" OPS_REMOTE="test-crypt:ops-vault-t10" \
  OPS_MEMORY_REMOTE="test-crypt:claude-memory-t10" OPS_MARKER="$MARKT10" run_snap)
RCT10=$?
check "T10: run with a pre-existing memory-sync marker exits 0" "$RCT10" "0"
MEMSYNC_MARKER_AFTER=$(cat "$MEMSYNC_MARKER" 2>/dev/null)
check "T10 (F7): memory-sync's own last-ok.json is byte-identical after the run" "$MEMSYNC_MARKER_AFTER" "$MEMSYNC_MARKER_BEFORE"
DEFAULT_MEM_MARKER="$T10_HOME/data-vaults/claude-memory-offsite/last-ok.json"
[ -s "$DEFAULT_MEM_MARKER" ] && ok "T10 (F7): offsite marker landed at the new default path (OPS_MEM_MARKER unset)" \
                              || bad "T10 (F7): no marker found at the new default path $DEFAULT_MEM_MARKER"
MARKER_LANE=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('lane',''))" "$DEFAULT_MEM_MARKER" 2>/dev/null)
check "T10 (F2e): marker lane is claude-memory" "$MARKER_LANE" "claude-memory"
MARKER_OFFSITE=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('offsite'))" "$DEFAULT_MEM_MARKER" 2>/dev/null)
check "T10 (F2e): marker offsite field is true" "$MARKER_OFFSITE" "True"
MARKER_FILES=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('files'))" "$DEFAULT_MEM_MARKER" 2>/dev/null)
FIXTURE_FILES=$(find "$T10_HOME/data-vaults/claude-memory" -type f | wc -l | tr -d ' ')
check "T10 (F2e): marker files count matches the fixture" "$MARKER_FILES" "$FIXTURE_FILES"
MARKER_BYTES=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('bytes'))" "$DEFAULT_MEM_MARKER" 2>/dev/null)
FIXTURE_BYTES=$(find "$T10_HOME/data-vaults/claude-memory" -type f -exec stat -f%z {} \; | awk '{s+=$1} END{print s+0}')
check "T10 (F2e): marker bytes count matches the fixture" "$MARKER_BYTES" "$FIXTURE_BYTES"

echo "== T11 (F3a pin): OPS_MARKER default path lands outside the vault tree, under \$HOME =="
VAULTT11="$WORK/vault-t11"
DEFAULT_OPS_MARKER="$FAKE_HOME/data-vaults/ops-vault/last-ok.json"
[ -e "$DEFAULT_OPS_MARKER" ] && bad "T11 setup: default marker path already exists before any run" \
                              || ok "T11 setup: default marker path absent before any run"
OUTT11A=$(OPS_VAULT="$VAULTT11" OPS_REMOTE="test-crypt:ops-vault-t11" \
  OPS_MEMORY_VAULT_OFFSITE=0 OPS_MEMORY_REMOTE="test-crypt:claude-memory" run_snap)
RCT11A=$?
check "T11: first run with no OPS_MARKER override exits 0" "$RCT11A" "0"
[ -s "$DEFAULT_OPS_MARKER" ] && ok "T11 (F3a): marker landed at the default path (\$HOME/data-vaults/ops-vault/last-ok.json)" \
                              || bad "T11 (F3a): no marker at the default path $DEFAULT_OPS_MARKER"
[ -e "$VAULTT11/data-vaults" ] && bad "T11: default marker path leaked INTO the vault tree" \
                                || ok "T11: default marker path is not inside the vault tree"
OUTT11B=$(OPS_VAULT="$VAULTT11" OPS_REMOTE="test-crypt:ops-vault-t11" \
  OPS_MEMORY_VAULT_OFFSITE=0 OPS_MEMORY_REMOTE="test-crypt:claude-memory" run_snap)
RCT11B=$?
check "T11: second unchanged run exits 0" "$RCT11B" "0"
echo "$OUTT11B" | grep -q "ops-snapshot: no changes at" \
  && ok "T11: unchanged second run prints 'no changes' with the default OPS_MARKER" \
  || bad "T11: unchanged second run did not print 'no changes': $OUTT11B"

echo "== T13: assert_allowlisted_remote itself fails on a broken or empty listing, never a vacuous ok =="
T13_RC_OUT=$( ( assert_allowlisted_remote "test-crypt:nonexistent-t13-remote/current" "$CLAUDE_ALLOWED" "T13 rc-fail probe" ) 2>&1 )
echo "$T13_RC_OUT" | grep -q "^  FAIL" \
  && ok "T13: assert_allowlisted_remote fails (not a vacuous ok) when the listing command errors" \
  || bad "T13: assert_allowlisted_remote did not fail on a broken listing: $T13_RC_OUT"
T13_EMPTYDIR="$WORK/vault-t13-empty"; mkdir -p "$T13_EMPTYDIR"
T13_EMPTY_OUT=$( ( assert_allowlisted_remote "test-plain:$T13_EMPTYDIR" "$CLAUDE_ALLOWED" "T13 empty-listing probe" ) 2>&1 )
echo "$T13_EMPTY_OUT" | grep -q "^  FAIL" \
  && ok "T13: assert_allowlisted_remote fails (not a vacuous ok) when the listing is empty" \
  || bad "T13: assert_allowlisted_remote did not fail on an empty listing: $T13_EMPTY_OUT"

echo "== T14a: MUTATION-PROVE T8 -- claude-memory copy gate replaced with a no-op ([ mcopy_rc -eq 0 ] || true, M05b) =="
MUTOLD_M05B="$WORK/mut-m05b-old.txt"; MUTNEW_M05B="$WORK/mut-m05b-new.txt"
printf '%s' '[ "$mcopy_rc" -eq 0 ] || die "rclone copy of claude-memory to $MEMORY_REMOTE (rc=$mcopy_rc)" "claude-memory"' > "$MUTOLD_M05B"
printf '%s' '[ "$mcopy_rc" -eq 0 ] || true' > "$MUTNEW_M05B"
BADBACKINGT14="$WORK/badbacking-t14"; mkdir -p "$BADBACKINGT14"
chmod -R 555 "$BADBACKINGT14"
PWT14=$(rclone obscure t14-pw-1); PWT14_2=$(rclone obscure t14-pw-2)
cat >> "$RCONF" <<CFG

[bad-crypt-t14]
type = crypt
remote = $BADBACKINGT14
password = $PWT14
password2 = $PWT14_2
CFG
if mutate_block "$MUTOLD_M05B" "$MUTNEW_M05B" "$SUBJECT"; then
  ok "mutation applied: claude-memory copy gate replaced with a no-op (M05b)"
  VAULTT14A="$WORK/vault-t14a"
  MARKT14A="$WORK/vault-t14a.marker.json"
  MEMMARKT14A="$WORK/vault-t14a.memmarker.json"
  OUTT14A=$(OPS_VAULT="$VAULTT14A" OPS_REMOTE="test-crypt:ops-vault-t14a" \
    OPS_MEMORY_REMOTE="bad-crypt-t14:sub" OPS_MARKER="$MARKT14A" OPS_MEM_MARKER="$MEMMARKT14A" run_snap)
  RCT14A=$?
  if echo "$OUTT14A" | grep -q "ops-snapshot: pushed claude-memory to"; then
    ok "MUTATION CONFIRMED (red, T8/M05b): with the copy gate defeated, a genuinely failed claude-memory copy is falsely reported as pushed -- $(echo "$OUTT14A" | grep 'pushed claude-memory to')"
  else
    bad "MUTATION did not reproduce the T8 defect (rc=$RCT14A): $OUTT14A"
  fi
  restore_subject "T14a (M05b)"
  VAULTT14G="$WORK/vault-t14g"
  MARKT14G="$WORK/vault-t14g.marker.json"
  MEMMARKT14G="$WORK/vault-t14g.memmarker.json"
  OUTT14G=$(OPS_VAULT="$VAULTT14G" OPS_REMOTE="test-crypt:ops-vault-t14g" \
    OPS_MEMORY_REMOTE="bad-crypt-t14:sub" OPS_MARKER="$MARKT14G" OPS_MEM_MARKER="$MEMMARKT14G" run_snap)
  RCT14G=$?
  if [ "$RCT14G" -eq 1 ] && ! echo "$OUTT14G" | grep -q "ops-snapshot: pushed claude-memory to"; then
    ok "restored script (green, T8): the same failing remote correctly exits 1 with no 'pushed claude-memory to' line -- $(echo "$OUTT14G" | grep 'rclone copy of claude-memory' | head -1)"
  else
    bad "restored script did not behave correctly on the same failing remote (rc=$RCT14G): $OUTT14G"
  fi
  chmod -R 755 "$BADBACKINGT14" 2>/dev/null
else
  bad "could not apply T14a mutation (block not found exactly once); script left untouched"
fi

echo "== T14b: MUTATION-PROVE T9 -- claude-memory failure lane branch disabled (F2a/F2b) =="
MUTOLD_T14B="$WORK/mut-t14b-old.txt"; MUTNEW_T14B="$WORK/mut-t14b-new.txt"
cat > "$MUTOLD_T14B" <<'BLOCK'
  if [ "$lane" = "claude-memory" ]; then
    title="claude-memory offsite failed: $msg"
    dedupe="claude-memory-offsite-failed"
    detail="The claude-memory offsite backup did not complete at $TS. ~/data-vaults/claude-memory has no offsite copy of its own until this leg succeeds again. Log: ~/.claude/failures/ops-snapshot.log"
  else
BLOCK
cat > "$MUTNEW_T14B" <<'BLOCK'
  if [ "$lane" = "__never__" ]; then
    title="claude-memory offsite failed: $msg"
    dedupe="claude-memory-offsite-failed"
    detail="The claude-memory offsite backup did not complete at $TS. ~/data-vaults/claude-memory has no offsite copy of its own until this leg succeeds again. Log: ~/.claude/failures/ops-snapshot.log"
  else
BLOCK
if mutate_block "$MUTOLD_T14B" "$MUTNEW_T14B" "$SUBJECT"; then
  ok "mutation applied: claude-memory failure lane branch disabled (F2a/F2b)"
  rm -f "$FT_LOG"
  HOME="$FT_HOME" OPS_HOME="$FT_HOME" RCLONE_CONFIG="$RCONF" \
    OPS_VAULT="$WORK/vault-t14b" OPS_REMOTE="test-crypt:ops-vault-t14b" \
    OPS_MEMORY_REMOTE="test-plain:$PLAINBACKING" OPS_MARKER="$WORK/vault-t14b.marker.json" \
    /bin/bash "$SUBJECT" >/dev/null 2>&1
  if grep -q -- "--dedupe-key claude-memory-offsite-failed" "$FT_LOG" 2>/dev/null; then
    bad "MUTATION did not reproduce the defect -- claude-memory dedupe key still filed correctly"
  else
    ok "MUTATION CONFIRMED (red, T9/F2a-F2b): with the lane branch disabled, a failing claude-memory leg files under the WRONG key -- $(cat "$FT_LOG" 2>/dev/null)"
  fi
  restore_subject "T14b (lane branch)"
  rm -f "$FT_LOG"
  HOME="$FT_HOME" OPS_HOME="$FT_HOME" RCLONE_CONFIG="$RCONF" \
    OPS_VAULT="$WORK/vault-t14b-green" OPS_REMOTE="test-crypt:ops-vault-t14b-green" \
    OPS_MEMORY_REMOTE="test-plain:$PLAINBACKING" OPS_MARKER="$WORK/vault-t14b-green.marker.json" \
    /bin/bash "$SUBJECT" >/dev/null 2>&1
  if grep -q -- "--dedupe-key claude-memory-offsite-failed" "$FT_LOG" 2>/dev/null; then
    ok "restored script (green, T9): failing claude-memory leg files correctly under claude-memory-offsite-failed"
  else
    bad "restored script did not file the correct dedupe key: $(cat "$FT_LOG" 2>/dev/null)"
  fi
else
  bad "could not apply T14b mutation (block not found exactly once); script left untouched"
fi

echo "== final sanity: restored subject still bash -n clean, byte-identical to backup, and the tracked $SCRIPT was never touched =="
/bin/bash -n "$SUBJECT" && ok "restored subject: bash -n clean" || bad "restored subject: bash -n FAILED"
diff -q "$BACKUP" "$SUBJECT" >/dev/null 2>&1 && ok "restored subject is byte-identical to the pre-mutation backup" || bad "restored subject differs from backup"
SCRIPT_HASH_AFTER=$(shasum "$SCRIPT" | cut -d' ' -f1)
check "T1: the tracked $SCRIPT was never written to by this suite" "$SCRIPT_HASH_AFTER" "$SCRIPT_HASH_BEFORE"

chmod -R 755 "$BADBACKINGPF" "$BADBACKINGPFM" "$BADBACKINGT8" "$BADBACKINGT14" 2>/dev/null
rm -rf "$BADBACKING1" "$BADBACKING2" "$BADBACKINGM" "$BADBACKINGPF" "$BADBACKINGPFM" "$BADBACKINGT8" "$BADBACKINGT14" 2>/dev/null

echo
echo "SUMMARY: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
