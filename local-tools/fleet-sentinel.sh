#!/bin/bash
# fleet-sentinel — daily event-time watchdog (com.jeffpinto.fleet-sentinel, 08:15).
# Catches at event time what audits kept finding late: dead launchd jobs, stale logs,
# expired credentials, repo rot. Every anomaly is filed via ~/.claude/bin/failtask
# (deduped board items on GitHub Project #1). Findings are batched: ONE board item
# per project per run, dedupe key = project + hash of its condition codes, so an
# unchanged anomaly set never re-files but a NEW condition does.
# Config: ~/.claude/failures/sentinel-config.json   Contract: ~/.claude/failures/README.md
set -u
shopt -s nullglob

HOME_DIR="$HOME"
BIN="$HOME_DIR/.claude/bin"
FAILDIR="$HOME_DIR/.claude/failures"
JSONL="$FAILDIR/failures.jsonl"
CONFIG="$FAILDIR/sentinel-config.json"
FAILTASK="$BIN/failtask"
FINDINGS="$(mktemp "${TMPDIR:-/tmp}/fleet-sentinel.XXXXXX")"
trap 'rm -f "$FINDINGS"' EXIT
PY=/usr/bin/python3
PULSE="$BIN/estate-pulse.py"

# --dry-run: print what WOULD be filed (base checks + estate-pulse), touch nothing.
DRYRUN=0
for a in "$@"; do [ "$a" = "--dry-run" ] && DRYRUN=1; done
DRY_ARG=""; [ "$DRYRUN" = "1" ] && DRY_ARG="--dry-run"

# file_task: the single filing gate. In dry-run it prints instead of calling failtask.
file_task() {  # file_task <project> <title> <detail> <dedupe-key> <severity>
  if [ "$DRYRUN" = "1" ]; then
    echo "  DRY-RUN would file [$1] $2 (key=$4 sev=$5)"
    return 0
  fi
  "$FAILTASK" "$1" "$2" --detail "$3" --dedupe-key "$4" --severity "$5"
}

mkdir -p "$FAILDIR"
echo "fleet-sentinel: run start $(date '+%Y-%m-%d %H:%M:%S')${DRYRUN:+ (dry-run=$DRYRUN)}"

add_finding() {  # add_finding <project> <code> <detail>
  printf '%s|%s|%s\n' "$1" "$2" "$3" >> "$FINDINGS"
  echo "  finding: [$1] $2 — $3"
}

proj_for_label() {
  case "$1" in
    com.jeff.mlb-*)                 echo mlb-hits-forecast ;;
    com.jeffpinto.lantern-*)        echo lantern ;;
    com.jeffpinto.atlas-*)          echo music-atlas ;;
    com.jeffpinto.crm-backup|com.jeffpinto.outbound-crm|com.jeffpinto.outbound-mint|com.jeffpinto.daily-sweep|com.jeffpinto.sweep-crm-import) echo outbound ;;
    com.jeffpinto.privacy-judge-*)  echo privacy-judge ;;
    com.jeffpinto.site-dev)         echo jeffpinto-site ;;
    com.jeffpinto.common-carrier-dev) echo common-carrier ;;
    com.jeffpinto.bluecamel-tunnel) echo bluecamel ;;
    com.jeffpinto.fleet-sentinel)   echo fleet-sentinel ;;
    com.jeffpinto.gh-board-export)  echo gh-board ;;
    *)                              echo infra ;;
  esac
}

# ---------- Check 5 (part A): self-test — was the previous run > 48h ago? ----------
prev_run=$("$PY" - "$JSONL" <<'PY'
import json, sys, os
path = sys.argv[1]
last = ""
if os.path.exists(path):
    with open(path) as f:
        for line in f:
            try: r = json.loads(line)
            except Exception: continue
            if r.get("type") == "sentinel-run":
                last = r.get("ts", "")
print(last)
PY
)
if [ -n "$prev_run" ]; then
  gap_ok=$("$PY" -c "
from datetime import datetime, timezone
prev = datetime.fromisoformat('$prev_run')
age_h = (datetime.now(timezone.utc) - prev.astimezone(timezone.utc)).total_seconds()/3600
print('stale' if age_h > 48 else 'ok'); print(round(age_h,1))" | head -1)
  if [ "$gap_ok" = "stale" ]; then
    add_finding fleet-sentinel sentinel-gap "previous sentinel run was $prev_run (>48h ago) — the watchdog itself went dark"
  fi
fi

# ---------- Check 1: launchd job health ----------
LCTL_OUT=$(/bin/launchctl list 2>/dev/null | grep -E 'com\.jeff(pinto)?\.' || true)
# NOTE: nonzero-last-exit-while-not-running is now CHECK A in estate-pulse (below),
# which files one rich, phone-readable card per job (label, code, stderr tail,
# kickstart cmd) at 4x/day cadence instead of a batched per-project rollup. The
# not-loaded / disabled scan stays here.

# plist present but not loaded (or explicitly disabled)
# launchd_ignore in sentinel-config.json = deliberately-manual jobs; never flag them.
LAUNCHD_IGNORE=$("$PY" -c "
import json,sys
try: print('\n'.join(json.load(open('$CONFIG')).get('launchd_ignore',[])))
except Exception: pass")
DISABLED=$(/bin/launchctl print-disabled "gui/$(id -u)" 2>/dev/null || true)
for plist in "$HOME_DIR"/Library/LaunchAgents/com.jeff*.plist "$HOME_DIR"/Library/LaunchAgents/com.jeff*.plist.disabled; do
  base=$(basename "$plist")
  label="${base%.plist.disabled}"; label="${label%.plist}"
  printf '%s\n' "$LAUNCHD_IGNORE" | grep -qx "$label" && continue
  if ! printf '%s\n' "$LCTL_OUT" | awk '{print $3}' | grep -qx "$label"; then
    p=$(proj_for_label "$label")
    case "$plist" in
      *.disabled) add_finding "$p" "launchd-off:$label" "$label: plist renamed .disabled — job is manual now" ;;
      *)
        if printf '%s\n' "$DISABLED" | grep -q "\"$label\" => disabled"; then
          add_finding "$p" "launchd-off:$label" "$label: disabled via launchctl — job is manual now"
        else
          add_finding "$p" "launchd-off:$label" "$label: plist present but NOT loaded — job is manual now"
        fi ;;
    esac
  fi
done

# ---------- Checks 2 + 3: log recency + credential ages (from config) ----------
"$PY" - "$CONFIG" <<'PY' >> "$FINDINGS"
import json, os, sys, time
cfg_path = sys.argv[1]
try:
    cfg = json.load(open(cfg_path))
except Exception as e:
    print(f"fleet-sentinel|config-broken|sentinel-config.json unreadable: {e}")
    sys.exit(0)
now = time.time()

def newest_mtime(path):
    if os.path.isdir(path):
        entries = [os.path.join(path, e) for e in os.listdir(path)]
        entries = [e for e in entries if os.path.isfile(e)]
        return max((os.path.getmtime(e) for e in entries), default=None)
    if os.path.isfile(path):
        return os.path.getmtime(path)
    return None

for spec in cfg.get("logs", []):
    path = os.path.expanduser(spec["path"])
    label = spec.get("label", path)
    m = newest_mtime(path)
    if m is None:
        print(f"{spec['project']}|log-missing:{os.path.basename(path)}|{label}: {path} does not exist")
        continue
    age_h = (now - m) / 3600
    if age_h > spec["max_age_hours"]:
        print(f"{spec['project']}|log-stale:{os.path.basename(path)}|{label}: {path} last written {age_h:.1f}h ago (max {spec['max_age_hours']}h) — its producer has silently stopped")

for spec in cfg.get("credentials", []):
    ttl = spec.get("ttl_days")
    if ttl is None:
        continue  # explicitly skipped
    path = os.path.expanduser(spec["path"])
    label = spec.get("label", path)
    if not os.path.isfile(path):
        print(f"{spec['project']}|cred-missing:{os.path.basename(path)}|{label}: {path} does not exist")
        continue
    age_d = (now - os.path.getmtime(path)) / 86400
    if age_d > ttl:
        print(f"{spec['project']}|cred-expired:{os.path.basename(path)}|{label}: {path} is {age_d:.1f}d old (TTL {ttl}d) — refresh the credential")
PY

# ---------- Check 4: repo hygiene quick pass over ~/Projects ----------
# repo_local_only in sentinel-config.json = repos that must NEVER reach a remote
# (client de-id airlocks, zero-egress corpora). Their commits are local BY DESIGN, so
# "push or rescue-push" is a false alarm that reappears every sweep; skip the unpushed
# check for them and keep every other hygiene check honest.
CUTOFF_48H=$(date -v-48H +%s)
REPO_LOCAL_ONLY=$("$PY" -c "
import json
try: print('\n'.join(json.load(open('$CONFIG')).get('repo_local_only',[])))
except Exception: pass")
for d in "$HOME_DIR"/Projects/*/; do
  [ -d "$d/.git" ] || continue
  repo=$(basename "$d")
  # unpushed commits on any branch (covers no-upstream branches too)
  unpushed=$(git -C "$d" log --branches --not --remotes --oneline 2>/dev/null | wc -l | tr -d ' ')
  if printf '%s\n' "$REPO_LOCAL_ONLY" | grep -qx "$repo"; then
    unpushed=0
  fi
  if [ "${unpushed:-0}" -gt 0 ]; then
    add_finding "$repo" "repo-unpushed" "$repo: $unpushed commit(s) exist on local branches only (not on any remote) — push or rescue-push"
  fi
  branch=$(git -C "$d" symbolic-ref --short -q HEAD || echo "")
  case "$branch" in
    main|master|primary)
      # dirty default-branch checkout where the dirt is older than 48h
      newest=0
      while IFS= read -r f; do
        [ -z "$f" ] && continue
        mt=$(stat -f %m "$d/$f" 2>/dev/null || echo 0)
        [ "$mt" -gt "$newest" ] && newest=$mt
      done < <(git -C "$d" status --porcelain 2>/dev/null | sed 's/^...//; s/.* -> //')
      if [ "$newest" -gt 0 ] && [ "$newest" -lt "$CUTOFF_48H" ]; then
        add_finding "$repo" "repo-dirty-stale" "$repo: dirty $branch checkout, newest change $(date -r "$newest" '+%Y-%m-%d %H:%M') (>48h) — commit to a branch or discard"
      fi ;;
  esac
  # default-branch pointer vs origin/HEAD
  ohead=$(git -C "$d" symbolic-ref -q refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||')
  if [ -n "$ohead" ]; then
    if ! git -C "$d" show-ref --verify -q "refs/heads/$ohead"; then
      add_finding "$repo" "repo-default-mismatch" "$repo: origin/HEAD points at '$ohead' but no local branch of that name exists"
    else
      behind=$(git -C "$d" rev-list --count "refs/heads/$ohead..refs/remotes/origin/$ohead" 2>/dev/null || echo 0)
      if [ "${behind:-0}" -gt 0 ]; then
        add_finding "$repo" "repo-default-behind" "$repo: local $ohead is $behind commit(s) behind origin/$ohead (as of last fetch) — sync the checkout"
      fi
    fi
  fi
done

# ---------- File findings ----------
# Repo-hygiene warns (repo-unpushed / repo-dirty-stale / repo-default-*) roll up into
# ONE standing board card (2026-07-21: they were 17% of the board as per-project cards).
# Everything else still files ONE failtask per project, deduped on condition-set.
n_findings=$(wc -l < "$FINDINGS" | tr -d ' ')
n_filed=0
if [ "$n_findings" -gt 0 ]; then
  ROLLUP=$(awk -F'|' '$2 ~ /^repo-(unpushed|dirty-stale|default-mismatch|default-behind)$/ {print "- " $3}' "$FINDINGS")
  while IFS= read -r proj; do
    codes=$(awk -F'|' -v p="$proj" '$1==p && $2 !~ /^repo-(unpushed|dirty-stale|default-mismatch|default-behind)$/ {print $2}' "$FINDINGS" | sort -u)
    [ -z "$codes" ] && continue
    details=$(awk -F'|' -v p="$proj" '$1==p && $2 !~ /^repo-(unpushed|dirty-stale|default-mismatch|default-behind)$/ {print "- " $3}' "$FINDINGS")
    codes_csv=$(printf '%s' "$codes" | tr '\n' ',' | sed 's/,$//')
    hash=$(printf '%s' "$codes_csv" | /sbin/md5 -q | cut -c1-12)
    sev=warn
    case "$codes_csv" in *launchd-exit*|*log-stale*|*log-missing*|*cred-expired*|*sentinel-gap*) sev=error ;; esac
    n=$(printf '%s\n' "$codes" | wc -l | tr -d ' ')
    file_task "$proj" "sentinel: $n issue(s) — $codes_csv" \
      "$details

Filed by fleet-sentinel (4x/day: 08/12/16/20 ET). Re-run: ~/.claude/bin/fleet-sentinel.sh
This item stays deduped while the condition set is unchanged; fix the causes and close it." \
      "sentinel:$proj:$hash" "$sev"
    n_filed=$((n_filed + 1))
  done < <(cut -d'|' -f1 "$FINDINGS" | sort -u)
  if [ -n "$ROLLUP" ]; then
    n_repo=$(printf '%s\n' "$ROLLUP" | wc -l | tr -d ' ')
    file_task ops-routines "sentinel: repo hygiene backlog ($n_repo finding(s))" \
      "$ROLLUP

Standing rollup of repo-hygiene warns (unpushed / dirty-stale / default-mismatch / default-behind).
Today's snapshot above; live truth = ~/.claude/failures/failures.jsonl + the weekly ~/Projects/_hygiene report.
This single card stays open as the pointer; per-repo fixes happen when a session touches that repo." \
      "sentinel:repo-hygiene-rollup" warn
    n_filed=$((n_filed + 1))
  fi
fi

# ---------- Check 6: estate-pulse (CHECK A launchd exits / B loop liveness / C stale PRs) ----------
# The 4x/day event-time layer added 2026-07-23 after three lanes stopped silently
# overnight (a stalled backfill nobody relaunched, a launchd job at exit 127 with no
# card, a staged board batch that never applied). Pure shell/sqlite/gh — NO LLM calls,
# so running it 4x/day forever costs nothing. Files via failtask; state in pulse-state.json.
if [ -f "$PULSE" ]; then
  echo "fleet-sentinel: running estate-pulse${DRY_ARG:+ $DRY_ARG}"
  "$PY" "$PULSE" $DRY_ARG || echo "fleet-sentinel: estate-pulse returned nonzero (non-fatal)"
else
  echo "fleet-sentinel: estate-pulse helper missing at $PULSE — skipping pulse checks"
fi

# ---------- Check 7: burn-meter (token burn per agent / session / model) ----------
# Added 2026-08-28 after the 27th/28th forensic: two sessions burned 97% of a 9.8B
# two-day total (97% of it cache-read) and nothing noticed until a hand audit. Pure
# transcript arithmetic, read-only, no LLM calls — safe at 4x/day. Thresholds live in
# sentinel-config.json under "burn"; breaches file their own deduped cards.
BURN="$BIN/burn-meter.py"
if [ -f "$BURN" ]; then
  echo "fleet-sentinel: running burn-meter${DRY_ARG:+ $DRY_ARG}"
  "$PY" "$BURN" --quiet $DRY_ARG || echo "fleet-sentinel: burn-meter returned nonzero (non-fatal)"
else
  echo "fleet-sentinel: burn-meter missing at $BURN — skipping burn checks"
fi

# ---------- Check 8: phoenix-enrich (normalize OTel spans for Phoenix) ----------
# Claude Code emits token counts as input_tokens/output_tokens/cache_*_tokens;
# Phoenix reads the OpenInference llm.token_count.* names, so every span showed
# zero tokens until 2026-08-28. Phoenix does not upsert re-sent spans, so the fix
# normalizes in place. 8h window covers the 4x/day cadence with overlap; it is
# idempotent, so an overlap re-scan costs a no-op pass. See
# ~/.claude/observability/README.md.
ENRICH="$BIN/phoenix-enrich.py"
if [ -f "$ENRICH" ]; then
  echo "fleet-sentinel: running phoenix-enrich${DRY_ARG:+ $DRY_ARG}"
  "$PY" "$ENRICH" --hours 8 --quiet $DRY_ARG || echo "fleet-sentinel: phoenix-enrich returned nonzero (non-fatal)"
else
  echo "fleet-sentinel: phoenix-enrich missing at $ENRICH — Phoenix token counts will read zero"
fi

# ---------- Check 9: heal-sweep (close FAILURE cards whose condition healed) ----------
# T-1735 / issue #739 (approved by Jeff 2026-08-31): a FAILURE card that outlives its
# own fix is habit-debt (creak ledger entry 5) — 30 were closed by hand on 2026-08-29.
# Reads the nightly gh-board export (never pages the live board for a sweep), classifies
# each open card's failkey into a machine-checkable heal class (launchd exit 0 + log
# recency, a quartet backup lane's last-ok.json, or memory-sync --check), and ONLY closes
# on real evidence — everything else is left open and counted as no-machine-checkable-heal.
# Report closes here so the heal stays loud, per memory fixes-that-quiet-alerts-leave-a-trace.
HEAL="$BIN/heal-sweep.py"
if [ -f "$HEAL" ]; then
  echo "fleet-sentinel: running heal-sweep${DRY_ARG:+ $DRY_ARG}"
  "$PY" "$HEAL" --quiet $DRY_ARG || echo "fleet-sentinel: heal-sweep returned nonzero (non-fatal)"
else
  echo "fleet-sentinel: heal-sweep missing at $HEAL — FAILURE cards will not self-close"
fi

# ---------- Check 5 (part B): log this run ----------
"$PY" - "$JSONL" "$n_findings" "$n_filed" <<'PY'
import json, sys, socket
from datetime import datetime, timezone
path, n_findings, n_filed = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
rec = {"ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
       "type": "sentinel-run", "findings": n_findings, "projects_filed": n_filed,
       "host": socket.gethostname()}
with open(path, "a") as f:
    f.write(json.dumps(rec, separators=(",", ":")) + "\n")
PY

echo "fleet-sentinel: done — $n_findings finding(s), $n_filed project item(s) filed. $(date '+%Y-%m-%d %H:%M:%S')"
exit 0
