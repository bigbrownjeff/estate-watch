#!/bin/bash
# Proves crash-evidence keeps a panic report once, records the boot-panic stamp,
# files a card per new panic, stays quiet on a second run, and fails loudly when
# the offsite push fails. Runs under /bin/bash 3.2 as launchd does.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
TOOL="${CRASH_EVIDENCE_TOOL:-$HERE/../crash-evidence}"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/src/Retired" "$T/bin"
printf '{"bug_type":"210","timestamp":"2026-11-18 09:00:37"}\npanic(cpu 0)\n' > "$T/src/panic-full-2026-11-18.ips"
printf '{"bug_type":"298","name":"JetsamEvent"}\n' > "$T/src/JetsamEvent-2026-11-21.ips"
printf 'old panic\n' > "$T/src/Retired/Kernel-2026-10-18.panic"
cat > "$T/bin/nvram" <<'N'
#!/bin/bash
cat "$STAMP_FILE"
N
cat > "$T/bin/rclone" <<'N'
#!/bin/bash
echo "$@" >> "$RCLONE_LOG"; exit "${RCLONE_RC:-0}"
N
cat > "$T/bin/failtask" <<'N'
#!/bin/bash
echo "$@" >> "$CARD_LOG"
N
chmod +x "$T/bin/"*
printf 'panicmedic-timestamps\t0:aaa\nboot-volume\tX\n' > "$T/stamp"
export STAMP_FILE="$T/stamp" RCLONE_LOG="$T/rclone.log" CARD_LOG="$T/cards.log"
export CRASH_SRC="$T/src" CRASH_DEST="$T/dest" CRASH_REMOTE="test:remote" NVRAM="$T/bin/nvram" RCLONE="$T/bin/rclone" FAILTASK="$T/bin/failtask"
: > "$RCLONE_LOG"; : > "$CARD_LOG"
fails=0
check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; fails=$((fails + 1)); fi; }

/bin/bash "$TOOL" > "$T/run1.out" 2>&1; rc1=$?
check "first run exits 0" '[ "$rc1" = 0 ]'
check "panic .ips kept" '[ -f "$T/dest/reports/panic-full-2026-11-18.ips" ]'
check "retired .panic kept" '[ -f "$T/dest/reports/Kernel-2026-10-18.panic" ]'
check "jetsam report not kept" '[ ! -f "$T/dest/reports/JetsamEvent-2026-11-21.ips" ]'
check "exactly two panic cards" '[ "$(grep -c "Kernel panic or forced reset" "$CARD_LOG")" = 2 ]'
check "first stamp recorded without a card" '[ "$(cat "$T/dest/panicmedic.last")" = "panicmedic-timestamps	0:aaa" ] && ! grep -q "Boot-panic stamp changed" "$CARD_LOG"'
check "one offsite push to the remote" '[ "$(grep -c "copy $T/dest test:remote" "$RCLONE_LOG")" = 1 ]'

/bin/bash "$TOOL" > "$T/run2.out" 2>&1; rc2=$?
check "second run exits 0 and adds nothing" '[ "$rc2" = 0 ] && grep -q "ok new=0" "$T/run2.out" && [ "$(wc -l < "$RCLONE_LOG" | tr -d " ")" = 1 ] && [ "$(wc -l < "$CARD_LOG" | tr -d " ")" = 2 ]'

printf 'panicmedic-timestamps\t0:bbb\n' > "$T/stamp"
/bin/bash "$TOOL" > "$T/run3.out" 2>&1; rc3=$?
check "changed stamp files one card and pushes" '[ "$rc3" = 0 ] && [ "$(grep -c "Boot-panic stamp changed" "$CARD_LOG")" = 1 ] && [ "$(wc -l < "$RCLONE_LOG" | tr -d " ")" = 2 ] && grep -q "0:bbb" "$T/dest/panicmedic.history" && grep -q "0:aaa" "$T/dest/panicmedic.history"'

printf 'panic\n' > "$T/src/second.panic"
RCLONE_RC=1 /bin/bash "$TOOL" > "$T/run4.out" 2>&1; rc4=$?
check "failed offsite push exits 1 and files a card" '[ "$rc4" = 1 ] && grep -q "offsite push failed" "$CARD_LOG"'

CRASH_SRC="$T/nope" /bin/bash "$TOOL" > "$T/run5.out" 2>&1; rc5=$?
check "missing source exits 1 and files a card" '[ "$rc5" = 1 ] && grep -q "cannot read" "$CARD_LOG"'

echo "RESULT fails=$fails"
[ "$fails" = 0 ]
