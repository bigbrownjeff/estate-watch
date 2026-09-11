#!/bin/bash
# Offline tests for gh-board-export.sh's comments.json capture (T-1742 lane L4,
# second half). Stubs `gh` and `board-ref` on PATH; no real subprocess ever
# touches GitHub or the real ~/.claude tree (HOME is redirected per test).
#
# Run: bash ~/.claude/bin/tests/test_gh_board_export_comments.sh
set -u
SCRIPT="$HOME/.claude/bin/gh-board-export.sh"
PASS=0
FAIL=0
check() {
  if [ "$2" = "1" ]; then PASS=$((PASS+1)); echo "ok   - $1"
  else FAIL=$((FAIL+1)); echo "FAIL - $1${3:+ :: $3}"; fi
}

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# ---- fake gh -----------------------------------------------------------------
FIX="$WORK/fixtures"
mkdir -p "$FIX"
cat > "$FIX/items.json" <<'JSON'
{"items": [{"id": "PVTI_a", "content": {"type": "Issue", "number": 1, "url": "x", "title": "t", "body": "b"}}], "totalCount": 1}
JSON
cat > "$FIX/fields.json" <<'JSON'
{"fields": [{"id": "F1", "name": "Status"}]}
JSON
cat > "$FIX/views.json" <<'JSON'
{"data": {"user": {"projectV2": {"views": {"nodes": [{"name": "Board", "layout": "BOARD_LAYOUT", "filter": ""}]}}}}}
JSON
printf '1\n2\n' > "$FIX/issue_numbers.txt"
cat > "$FIX/comments_1.jsonl" <<'JSONL'
{"id": 900, "author": "bigbrownjeff", "createdAt": "2026-08-01T00:00:00Z", "body": "first"}
{"id": 901, "author": "bigbrownjeff", "createdAt": "2026-08-02T00:00:00Z", "body": "second"}
JSONL
: > "$FIX/comments_2.jsonl"

BIN="$WORK/bin"
mkdir -p "$BIN"
cat > "$BIN/gh" <<EOG
#!/bin/bash
set -u
FIX="$FIX"
if [ "\$1" = "project" ] && [ "\$2" = "item-list" ]; then cat "\$FIX/items.json"; exit 0; fi
if [ "\$1" = "project" ] && [ "\$2" = "field-list" ]; then cat "\$FIX/fields.json"; exit 0; fi
if [ "\$1" = "api" ] && [ "\$2" = "graphql" ]; then cat "\$FIX/views.json"; exit 0; fi
if [ "\$1" = "api" ]; then
  shift  # drop "api"
  if [ "\$1" = "-X" ]; then shift 2; fi  # drop "-X GET" if present, per the real call shape
  PATH_ARG="\$1"
  case "\$PATH_ARG" in
    */issues)
      if [ "\${FAKE_GH_ISSUES_LIST_FAIL:-0}" = "1" ]; then
        echo "simulated failure listing issues" >&2
        exit 1
      fi
      cat "\$FIX/issue_numbers.txt"
      exit 0
      ;;
    */issues/*/comments)
      n=\$(echo "\$PATH_ARG" | sed -E 's#.*/issues/([0-9]+)/comments#\1#')
      [ -f "\$FIX/comments_\$n.jsonl" ] && cat "\$FIX/comments_\$n.jsonl"
      exit 0
      ;;
  esac
fi
echo "fake gh: unhandled args: \$*" >&2
exit 1
EOG
chmod +x "$BIN/gh"

cat > "$BIN/board-ref" <<'EOB'
#!/bin/bash
exit 0
EOB
chmod +x "$BIN/board-ref"

cat > "$BIN/failtask-noop" <<'EOB'
#!/bin/bash
exit 0
EOB
chmod +x "$BIN/failtask-noop"

run_export() {
  # $1 = vault dir, remaining = extra env assignments (VAR=val ...)
  local vault="$1"; shift
  env -i PATH="/usr/bin:/bin" HOME="$WORK/fakehome" \
      GH_BOARD_TEST_PATH="$BIN:/usr/bin:/bin" \
      GH_BOARD_OWNER=bigbrownjeff GH_BOARD_NUMBER=1 \
      GH_BOARD_VAULT="$vault" GH_BOARD_OFFSITE=0 \
      GH_BOARD_REF_BIN="$BIN/board-ref" GH_BOARD_FAILTASK="$BIN/failtask-noop" \
      "$@" bash "$SCRIPT"
}

# ---------------------------------------------------------------- (a)
VAULT_A="$WORK/vault-a"
run_export "$VAULT_A" >"$WORK/a.out" 2>&1
RC_A=$?
DEST_A="$VAULT_A/$(date '+%Y-%m-%d')"
check "a: exits 0" "$([ "$RC_A" -eq 0 ] && echo 1 || echo 0)" "rc=$RC_A $(cat "$WORK/a.out")"
for f in items.json fields.json views.json comments.json manifest.json; do
  check "a: $f exists" "$([ -s "$DEST_A/$f" ] && echo 1 || echo 0)"
done
check "a: manifest names all four in sha256" "$(python3 -c "
import json
m = json.load(open('$DEST_A/manifest.json'))
print(1 if set(m['sha256']) == {'items.json','fields.json','views.json','comments.json'} else 0)
")"
check "a: comments.json carries issue 1's thread" "$(python3 -c "
import json
c = json.load(open('$DEST_A/comments.json'))
print(1 if len(c['issues'].get('1', [])) == 2 else 0)
")"
check "a: comment_total is 2" "$(python3 -c "
import json
print(1 if json.load(open('$DEST_A/comments.json'))['comment_total'] == 2 else 0)
")"

# ---------------------------------------------------------------- (b)
VAULT_B="$WORK/vault-b"
run_export "$VAULT_B" GH_BOARD_REPO="" >"$WORK/b.out" 2>&1
RC_B=$?
DEST_B="$VAULT_B/$(date '+%Y-%m-%d')"
check "b: exits 0" "$([ "$RC_B" -eq 0 ] && echo 1 || echo 0)" "rc=$RC_B $(cat "$WORK/b.out")"
for f in items.json fields.json views.json manifest.json; do
  check "b: $f exists" "$([ -s "$DEST_B/$f" ] && echo 1 || echo 0)"
done
check "b: comments.json is NOT written" "$([ ! -e "$DEST_B/comments.json" ] && echo 1 || echo 0)"
check "b: manifest carries the explicit skip string" "$(python3 -c "
import json
m = json.load(open('$DEST_B/manifest.json'))
print(1 if m['comments'] == 'skipped (no board repo configured)' else 0)
")"
check "b: skip line present in output" "$(grep -q 'comments capture skipped' "$WORK/b.out" && echo 1 || echo 0)"

# ---------------------------------------------------------------- (c)
VAULT_C="$WORK/vault-c"
run_export "$VAULT_C" FAKE_GH_ISSUES_LIST_FAIL=1 >"$WORK/c.out" 2>&1
RC_C=$?
DEST_C="$VAULT_C/$(date '+%Y-%m-%d')"
check "c: exits nonzero" "$([ "$RC_C" -ne 0 ] && echo 1 || echo 0)" "rc=$RC_C"
check "c: names the reason" "$(grep -q 'listing issues in .* failed' "$WORK/c.out" && echo 1 || echo 0)" "$(cat "$WORK/c.out")"
for f in items.json fields.json views.json; do
  check "c: $f left intact" "$([ -s "$DEST_C/$f" ] && echo 1 || echo 0)"
done
check "c: manifest.json NOT written (die before it)" "$([ ! -e "$DEST_C/manifest.json" ] && echo 1 || echo 0)"

# ---------------------------------------------------------------- (d)
check "d: bash -n on the real bash" "$(bash -n "$SCRIPT" 2>/dev/null && echo 1 || echo 0)"
check "d: bash -n under /bin/bash (3.2, launchd's shell)" "$(/bin/bash -n "$SCRIPT" 2>/dev/null && echo 1 || echo 0)"
check "d: no bare \"\${arr[@]}\" array expansion (bash 3.2 + set -u trap)" "$(grep -qE '"\$\{[A-Za-z_]+\[@\]\}"' "$SCRIPT" && echo 0 || echo 1)"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
