#!/bin/bash
# Proves the log-recency check skips a spec whose launchd_label names a retired job
# (no plist in ~/Library/LaunchAgents) and still fires for the same stale log when the
# spec carries no label. Extracts the inline Python block from fleet-sentinel.sh and
# runs it against a temp HOME. Run: bash local-tools/tests/test_fleet_sentinel_retired_log.sh
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/../fleet-sentinel.sh"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
sed -n '/^"\$PY" - "\$CONFIG" <<'"'"'PY'"'"' >> "\$FINDINGS"$/,/^PY$/p' "$src" | sed '1d;$d' > "$tmp/check.py"
[ -s "$tmp/check.py" ] || { echo "FAIL: could not extract the log-recency Python block"; exit 1; }
mkdir -p "$tmp/home/Library/LaunchAgents"
printf 'old\n' > "$tmp/stale.log"; touch -t 202501010000 "$tmp/stale.log"
cat > "$tmp/cfg.json" <<JSON
{"logs": [
  {"project": "p", "label": "retired", "path": "$tmp/stale.log", "max_age_hours": 1, "launchd_label": "com.test.retired"},
  {"project": "p", "label": "live",    "path": "$tmp/stale.log", "max_age_hours": 1, "launchd_label": "com.test.live"},
  {"project": "p", "label": "bare",    "path": "$tmp/stale.log", "max_age_hours": 1}
], "credentials": []}
JSON
touch "$tmp/home/Library/LaunchAgents/com.test.live.plist"
out="$(HOME="$tmp/home" python3 "$tmp/check.py" "$tmp/cfg.json")"
rc=0
grep -q '|log-stale:stale.log|live:' <<<"$out" || { echo "FAIL: live job's stale log did not fire"; rc=1; }
grep -q '|log-stale:stale.log|bare:' <<<"$out" || { echo "FAIL: unlabelled stale log did not fire"; rc=1; }
grep -q 'retired:' <<<"$out" && { echo "FAIL: retired job's log still fired"; rc=1; }
[ $rc -eq 0 ] && echo "PASS: retired producer skipped, live and unlabelled producers still fire"
exit $rc
