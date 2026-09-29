#!/bin/bash
# test_ops_snapshot -- prove local-tools/ops-snapshot.sh covers the estate and
# the offsite push cannot lie about its own result.
#
# Everything here runs through OPS_HOME / OPS_VAULT / OPS_REMOTE /
# OPS_MEMORY_REMOTE / OPS_NO_FAILTASK pointed at a mktemp fixture home and an
# isolated RCLONE_CONFIG defining a real crypt remote over a local temp
# directory (no network, no real credentials). The real ~/.claude, the real
# ~/ops-vault, and the real gdw-crypt: remote are never touched.
#
# Style follows notes-vault/tests/test_data_snapshot.sh (ok/bad/check
# helpers, SUMMARY line, mutation-prove by editing a COPY and restoring from
# a cp'd backup -- never git checkout, never git stash).
#
# Run: bash local-tools/tests/test_ops_snapshot.sh
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$HERE/ops-snapshot.sh"
[ -x "$SCRIPT" ] || [ -f "$SCRIPT" ] || { echo "FAIL: $SCRIPT not found"; exit 1; }

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

# ---- isolated rclone config: a real crypt remote over a local backing dir,
# plus a non-crypt remote for case g. No network, no real credentials. ----
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
echo "regular script"   > "$FAKE_HOME/.local/bin/real-script.sh"
chmod +x "$FAKE_HOME/.local/bin/real-script.sh"
echo "SYMLINK_TARGET_CANARY_TEXT" > "$FAKE_HOME/.local/other/realbin"
ln -s "$FAKE_HOME/.local/other/realbin" "$FAKE_HOME/.local/bin/claude"
echo "memory snapshot 1" > "$FAKE_HOME/data-vaults/claude-memory/mem1.bin"

# Positive-only allowlist of what may exist under $VAULT/claude and
# $VAULT/home after a run. A denylist of the forbidden names would put
# "secrets" and ".env" as literal strings into this committed test for no
# reason; the allowlist form fails on anything unexpected instead, which is
# the case actually feared here.
CLAUDE_ALLOWED="bin hooks agents agent-memory skills commands failures links todos bundles handoffs-archive memory handoffs observability jobs scheduled scheduled-tasks watch state"
HOME_ALLOWED="local-bin"
assert_allowlisted() {  # $1 dir to list  $2 space-separated allowed names  $3 label
  local dir="$1" allowed=" $2 " label="$3" bad_names=""
  [ -d "$dir" ] || return 0
  for entry in "$dir"/*; do
    [ -e "$entry" ] || continue
    local name; name=$(basename "$entry")
    case "$allowed" in
      *" $name "*) : ;;
      *) bad_names="$bad_names $name" ;;
    esac
  done
  if [ -z "$bad_names" ]; then ok "$label: only allowlisted names present"
  else bad "$label: unexpected name(s) present:$bad_names"; fi
}

# Helper: keyed by the WHOLE fixture home tree so a script that reads only is
# provably a script that never writes. Symlinks are recorded by their target
# path, not dereferenced (a dangling one would break shasum).
snapshot_home() {
  find "$1" \( -type f -o -type l \) 2>/dev/null | sort | while read -r f; do
    if [ -L "$f" ]; then echo "L $f -> $(readlink "$f")"
    else echo "F $f $(shasum "$f" 2>/dev/null | cut -d' ' -f1)"; fi
  done | shasum | cut -d' ' -f1
}

run_snap() {  # env vars set by caller, positional: label
  OPS_HOME="$FAKE_HOME" RCLONE_CONFIG="$RCONF" OPS_NO_FAILTASK=1 \
    /bin/bash "$SCRIPT" "$@" 2>&1
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
BACKUP="$WORK/ops-snapshot.orig.sh"
cp "$SCRIPT" "$BACKUP"

echo "== 1. first full run =="
VAULT1="$WORK/vault1"
home_before=$(snapshot_home "$FAKE_HOME")
OUT1=$(OPS_VAULT="$VAULT1" OPS_REMOTE="test-crypt:ops-vault" \
       OPS_MEMORY_REMOTE="test-crypt:claude-memory" run_snap)
RC1=$?
check "first run exits 0" "$RC1" "0"
if [ "$RC1" -ne 0 ]; then
  echo "$OUT1"
  echo "SUMMARY: $pass passed, $((fail+1)) failed"
  exit 1
fi
home_after=$(snapshot_home "$FAKE_HOME")

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

echo "== 2c. allowlist assertion (positive form): only known names present =="
assert_allowlisted "$VAULT1/claude" "$CLAUDE_ALLOWED" "vault claude/ top level"
assert_allowlisted "$VAULT1/home"   "$HOME_ALLOWED"   "vault home/ top level"
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
[ -s "$VAULT1/last-ok.json" ] && ok "last-ok.json written after a clean run" || bad "last-ok.json missing after a clean run"

echo "== e. per-file verify: an offsite object goes missing behind the script's back =="
rclone delete "test-crypt:ops-vault/current/claude/handoffs/note1.md" >/dev/null 2>&1
GONE=$(rclone lsjson --recursive test-crypt:ops-vault/current 2>&1)
echo "$GONE" | grep -q '"Path":"claude/handoffs/note1.md"' \
  && bad "setup error: file still present after deliberate delete" \
  || ok "setup: offsite copy of note1.md deliberately removed"
OUT2=$(OPS_VAULT="$VAULT1" OPS_REMOTE="test-crypt:ops-vault" \
       OPS_MEMORY_REMOTE="test-crypt:claude-memory" run_snap)
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
       OPS_MEMORY_REMOTE="test-crypt:claude-memory" run_snap)
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
       OPS_MEMORY_REMOTE="test-crypt:claude-memory" run_snap)
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

echo "== c. OPS_MEMORY_VAULT_OFFSITE=0 skips the claude-memory leg entirely =="
VAULTG="$WORK/vault-memoff"
echo "memory snapshot only-local" > "$FAKE_HOME/data-vaults/claude-memory/mem3-localonly.bin"
OUT5=$(OPS_VAULT="$VAULTG" OPS_REMOTE="test-crypt:ops-vault" \
       OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 run_snap)
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
OUT6=$(OPS_VAULT="$VAULTP" OPS_REMOTE="test-plain:$PLAINBACKING" \
       OPS_MEMORY_REMOTE="test-crypt:claude-memory" run_snap)
RC6=$?
check "non-crypt OPS_REMOTE is refused" "$RC6" "1"
echo "$OUT6" | grep -qi "not crypt" && ok "refusal names the remote type" || bad "refusal message missing 'not crypt'"
[ -s "$VAULTP/last-ok.json" ] && bad "last-ok.json written despite refused offsite" || ok "no last-ok.json on refused offsite"
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
      --exclude '.DS_Store' --transfers 4 --timeout 5m 2>&1)
sync_rc=$?
printf '%s\n' "$sync_out" | tail -3
[ "$sync_rc" -eq 0 ] || die "rclone sync to $REMOTE (rc=$sync_rc)"
BLOCK
cat > "$MUTNEW" <<'BLOCK'
if rclone sync "$VAULT" "$REMOTE/current" \
      --backup-dir "$REMOTE/replaced/$STAMP" \
      --exclude '.DS_Store' --transfers 4 --timeout 5m 2>&1 | tail -3; then
  :
else
  die "rclone sync to $REMOTE"
fi
BLOCK
if mutate_block "$MUTOLD" "$MUTNEW" "$SCRIPT"; then
  ok "mutation applied: reintroduced the swallowed-exit-status pipe bug"
  VAULTM="$WORK/vault-mutant-d"
  OUTM=$(OPS_VAULT="$VAULTM" OPS_REMOTE="bad-crypt-1:sub" \
         OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 run_snap)
  RCM=$?
  # The script has TWO independent layers against a bad push: the sync
  # step's own rc capture, and a separate downstream `rclone check` that
  # runs regardless. Mutating only the first still lets the second layer
  # catch the resulting drift and fail the run overall (rc=1) -- so the
  # precise symptom of THIS defect is the sync step's own false claim of
  # success, not necessarily the script's final exit code.
  if [ "$RCM" -eq 0 ] && [ -s "$VAULTM/last-ok.json" ]; then
    ok "MUTATION CONFIRMED (full false-positive): rc=0 and last-ok.json written despite a failed push"
  elif echo "$OUTM" | grep -q "ops-snapshot: pushed to"; then
    ok "MUTATION CONFIRMED: the sync step itself falsely claims \"pushed to ...\" despite rclone reporting errors underneath -- exactly the historical swallowed-exit-status defect. (The run still ends nonzero here only because the separate, unmutated 'rclone check' step independently catches the resulting drift -- a second layer, not a substitute for capturing sync's own exit status.)"
  else
    bad "mutation did not reproduce the swallowed-exit-status defect at all (rc=$RCM) -- fixture or mutation is wrong"
  fi
  cp "$BACKUP" "$SCRIPT"
  diff -q "$BACKUP" "$SCRIPT" >/dev/null 2>&1 && ok "script restored from backup after case d mutation" || bad "script NOT restored after case d mutation"
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
OUTR=$(OPS_VAULT="$VAULTR" OPS_REMOTE="bad-crypt-2:sub" \
       OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 run_snap)
RCR=$?
check "REAL (restored) script: failed push exits nonzero" "$RCR" "1"
[ -s "$VAULTR/last-ok.json" ] && bad "REAL script wrote last-ok.json despite a failed push" \
                              || ok "REAL script correctly wrote no last-ok.json on a failed push"
chmod -R 755 "$BADBACKING1" "$BADBACKING2" 2>/dev/null

echo "== b. MUTATION-PROVE: .local/bin symlink handling =="
MUTOLD_B="$WORK/mut-b-old.txt"; MUTNEW_B="$WORK/mut-b-new.txt"
printf '%s' 'rsync -a --no-links --delete --exclude '"'"'.DS_Store'"'"' \' > "$MUTOLD_B"
printf '%s' 'rsync -aL --delete --exclude '"'"'.DS_Store'"'"' \' > "$MUTNEW_B"
if mutate_block "$MUTOLD_B" "$MUTNEW_B" "$SCRIPT"; then
  ok "mutation applied: .local/bin rsync now follows symlinks (-L instead of --no-links)"
  VAULTB="$WORK/vault-mutant-b"
  OUTB=$(OPS_VAULT="$VAULTB" OPS_REMOTE="test-crypt:ops-vault-b" \
         OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 run_snap)
  if grep -rq "SYMLINK_TARGET_CANARY_TEXT" "$VAULTB" 2>/dev/null; then
    ok "MUTATION CONFIRMED: following the symlink leaks its target content into the vault -- this test would have caught it"
  else
    bad "mutation did not reproduce the symlink-following defect -- fixture or mutation is wrong"
  fi
  cp "$BACKUP" "$SCRIPT"
  diff -q "$BACKUP" "$SCRIPT" >/dev/null 2>&1 && ok "script restored from backup after case b mutation" || bad "script NOT restored after case b mutation"
else
  bad "could not apply case b mutation (block not found exactly once); script left untouched"
fi

echo "== c. MUTATION-PROVE: an unlisted directory sneaking into the vault =="
MUTOLD_C="$WORK/mut-c-old.txt"; MUTNEW_C="$WORK/mut-c-new.txt"
printf '%s' 'sync_dir "$OPS_HOME/.claude/handoffs/"        "claude/handoffs/"' > "$MUTOLD_C"
{ printf '%s\n' 'sync_dir "$OPS_HOME/.claude/handoffs/"        "claude/handoffs/"'
  printf '%s' 'sync_dir "$OPS_HOME/.claude/secrets/" "claude/secrets/"'; } > "$MUTNEW_C"
if mutate_block "$MUTOLD_C" "$MUTNEW_C" "$SCRIPT"; then
  ok "mutation applied: secrets/ added to the sync allowlist"
  VAULTC="$WORK/vault-mutant-c"
  OUTC=$(OPS_VAULT="$VAULTC" OPS_REMOTE="test-crypt:ops-vault-c" \
         OPS_MEMORY_REMOTE="test-crypt:claude-memory" OPS_MEMORY_VAULT_OFFSITE=0 run_snap)
  if [ -e "$VAULTC/claude/secrets" ]; then
    ok "MUTATION CONFIRMED: secrets/ now present under vault claude/ -- the allowlist-membership assertion (2c above) would flag this as an unexpected name and fail"
  else
    bad "mutation did not reproduce the leak -- fixture or mutation is wrong"
  fi
  cp "$BACKUP" "$SCRIPT"
  diff -q "$BACKUP" "$SCRIPT" >/dev/null 2>&1 && ok "script restored from backup after case c mutation" || bad "script NOT restored after case c mutation"
else
  bad "could not apply case c mutation (block not found exactly once); script left untouched"
fi

echo "== final sanity: restored script still bash -n clean and still passes the good path =="
/bin/bash -n "$SCRIPT" && ok "restored script: bash -n clean" || bad "restored script: bash -n FAILED"
diff -q "$BACKUP" "$SCRIPT" >/dev/null 2>&1 && ok "restored script is byte-identical to the pre-mutation backup" || bad "restored script differs from backup"

rm -rf "$BADBACKING1" "$BADBACKING2" 2>/dev/null

echo
echo "SUMMARY: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
