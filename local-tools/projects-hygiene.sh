#!/bin/bash
# Weekly ~/Projects hygiene audit (com.jeffpinto.projects-hygiene).
# Writes a dated local report and a desktop notification. It never changes a Git
# repository, removes a worktree/branch, or files an external task.
set -u

report_dir="$HOME/Projects/_hygiene"
mkdir -p "$report_dir"
run_date=$(date '+%Y-%m-%d')
report="$report_dir/$run_date.md"
facts=$("$HOME/.claude/bin/projects-hygiene-facts.sh" 2>/dev/null)

{
  echo "# Projects hygiene — $run_date"
  echo
  echo "## Unpushed commits"
  echo "$facts" | awk -F'|' '$8+0 > 0 {printf "- **%s** (%s): %s unpushed on %s\n", $1, $2, $8, $4}'
  echo
  echo "## No remote / no upstream"
  # Intentionally-local repos are excluded: mattel-airlock is a zero-egress
  # declassification vault (raw client-confidential docs; a remote would BE the
  # leak). Adding a repo here requires the same justification in this comment.
  echo "$facts" | awk -F'|' '$8=="no-upstream" && $1 != "mattel-airlock" {printf "- %s (branch %s)\n", $1, $4}'
  echo
  echo "## Stale worktrees (last commit >14 days)"
  echo "$facts" | awk -F'|' -v cutoff="$(date -v-14d '+%Y-%m-%d')" '$2=="worktree" && $9 < cutoff && $9 != "-" {printf "- %s (%s, last %s)\n", $1, $4, $9}'
  echo
  echo "## Dirty trees (uncommitted changes)"
  echo "$facts" | awk -F'|' '$6+0 > 0 || $7+0 > 3 {printf "- %s: %s modified, %s untracked\n", $1, $6, $7}'
  echo
  echo "## Handoffs missing from the Notes Vault"
  # Cheap deterministic coverage check (2026-08-22 sweep found 77% uncovered).
  # note_id convention: session-<handoff filename stem>. Best-effort: skips if
  # the vault DB is not on this machine.
  vault_db="$HOME/Projects/notes-vault/data/notes.db"
  if [ -f "$vault_db" ]; then
    # Worktree checkouts duplicate their canonical repo's handoffs; exclude the
    # estate's worktree conventions so they don't read as cross-repo collisions.
    handoff_files=$( { find "$HOME/Projects" -path '*/.claude/handoffs/*.md' \
                         -not -path '*/_wt/*' -not -path '*/.worktrees/*' \
                         -not -path '*/_worktrees/*' -not -path '*-wt/*' \
                         -not -path '*-worktrees/*' -not -path '*/.wt/*' 2>/dev/null; \
                       find "$HOME/.claude/handoffs" -name '*.md' 2>/dev/null; } )
    expected=$(echo "$handoff_files" | sed 's|.*/||; s|\.md$||; s|^|session-|' | sort -u)
    # Same filename in two repos collapses to one expected id; list those stems
    # explicitly instead of silently false-negativing (2026-08-22: 3 repos shared
    # "2026-07-18-initial-build" and hid behind one disambiguated set of notes).
    collisions=$(echo "$handoff_files" | sed 's|.*/||; s|\.md$||' | sort | uniq -d)
    ambiguous=""
    for stem in $collisions; do
      # Byte-identical copies are duplicate checkouts, not ambiguity.
      n=$(echo "$handoff_files" | grep "/$stem\.md$" | while IFS= read -r f; do md5 -q "$f"; done | sort -u | wc -l | tr -d ' ')
      [ "$n" -gt 1 ] && ambiguous="$ambiguous$stem"$'\n'
    done
    if [ -n "$ambiguous" ]; then
      echo "Ambiguous (same handoff filename, DIFFERENT content, in multiple repos; verify vault coverage by hand):"
      echo "$ambiguous" | sed '/^$/d; s/^/- /'
    fi
    logged=$(sqlite3 "$vault_db" "SELECT source_id FROM notes WHERE source_app='session'" 2>/dev/null | sort -u)
    missing=$(comm -23 <(echo "$expected") <(echo "$logged") | sed '/^$/d')
    missing_n=$(echo "$missing" | sed '/^$/d' | wc -l | tr -d ' ')
    if [ "$missing_n" -gt 0 ]; then
      echo "$missing_n handoff(s) with no vault session note (run the vault-keeper sweep):"
      echo "$missing" | head -10 | sed 's/^/- /'
    else
      echo "All handoffs covered."
    fi
  else
    echo "Vault DB not found; check skipped."
  fi
  echo
  echo "_Full fact table:_"
  echo '```'
  echo "$facts"
  echo '```'
} > "$report"

unpushed=$(echo "$facts" | awk -F'|' '$8+0>0 {s+=$8} END {print s+0}')
no_remote=$(echo "$facts" | awk -F'|' '$8=="no-upstream"' | wc -l | tr -d ' ')
/usr/bin/osascript -e "display notification \"$unpushed unpushed commits, $no_remote untracked branches — $run_date.md\" with title \"Projects hygiene\"" 2>/dev/null

# Reporting only: sqlite-lint may refresh its local findings report, but cannot
# file a board card because --failtask is intentionally absent.
{
  echo
  echo "## SQLite lock convention"
  echo '```'
  /usr/bin/python3 "$HOME/.claude/bin/sqlite-lint.py" 2>&1
  echo '```'
} >> "$report" || true

# Preview candidates only. Applying removals is a separate, explicitly requested
# operation after a human/agent rechecks exact paths and dirty state.
{
  echo
  echo "## Worktree janitor (dry run)"
  echo '```'
  /usr/bin/python3 "$HOME/.claude/bin/worktree-janitor.py" --dry-run 2>&1
  echo '```'
} >> "$report" || true

# Disk headroom (2026-08-27: added after 43 GB free / 926 GB triggered a
# manual groundskeeper inventory — the weekly sweep hadn't been watching
# free space or cache/venv bloat at all).
#
# Report-only, forever: this section never deletes or moves a byte. It sizes
# up candidates and loudly flags when free space gets tight, so shrinking
# headroom shows up on a Sunday instead of the day someone needs 60 GB for a
# VM and has 43. The full du scan is cached and time-boxed so a slow disk
# never wedges the weekly job (empty-results-must-speak: a killed/missing
# scan says so explicitly, it never silently renders as "nothing to report").
{
  echo
  echo "## Disk headroom"
  echo

  disk_state_dir="$HOME/.claude/state"
  mkdir -p "$disk_state_dir"
  disk_cache="$disk_state_dir/disk-headroom-du-cache.txt"
  disk_cache_meta="$disk_state_dir/disk-headroom-du-cache.meta"
  du_budget_secs=300   # 5-minute cap on the full-tree du scan

  free_bytes=$(diskutil info -plist / 2>/dev/null | plutil -extract APFSContainerFree raw -o - - 2>/dev/null)
  if [ -n "${free_bytes:-}" ]; then
    free_gb=$(( free_bytes / 1000000000 ))
    echo "**Free space:** ${free_gb} GB"
  else
    echo "**Free space:** COULD NOT READ (diskutil/plutil call failed) — check manually, do not assume OK"
    free_gb=999
  fi

  purg_line=$(diskutil info / 2>/dev/null | grep -i purgeable || true)
  if [ -n "$purg_line" ]; then
    echo "**Purgeable:** $purg_line"
  else
    echo "**Purgeable:** not exposed by \`diskutil info\` on this macOS version (checked $run_date) — not the same as zero"
  fi
  echo

  if [ "$free_gb" -lt 50 ]; then
    echo "**FREE SPACE CRITICAL: ${free_gb} GB < 50 GB.**"
    "$HOME/.claude/bin/failtask" disk-headroom \
      "Free disk space under 50 GB (${free_gb} GB free)" \
      --detail "See $report for the top offenders (Disk headroom section)." \
      --dedupe-key "disk-headroom-critical" 2>&1 || true
  elif [ "$free_gb" -lt 100 ]; then
    echo "**FREE SPACE LOW: ${free_gb} GB, under the 100 GB headroom target.**"
  fi
  echo

  # Cached, time-boxed du of the two biggest trees. Refresh at most once/24h;
  # if a refresh is due but blows the budget, kill it and say so — never
  # silently fall through to an empty or stale-looking table.
  need_refresh=1
  if [ -f "$disk_cache_meta" ]; then
    last_run=$(cat "$disk_cache_meta" 2>/dev/null || echo 0)
    now=$(date +%s)
    age=$(( now - last_run ))
    [ "$age" -lt 86400 ] && need_refresh=0
  fi

  if [ "$need_refresh" -eq 1 ]; then
    tmp_out=$(mktemp)
    ( du -xd1 -h "$HOME/Projects" "$HOME/Library" 2>/dev/null ) > "$tmp_out" &
    du_pid=$!
    waited=0
    while kill -0 "$du_pid" 2>/dev/null && [ "$waited" -lt "$du_budget_secs" ]; do
      sleep 5
      waited=$((waited + 5))
    done
    if kill -0 "$du_pid" 2>/dev/null; then
      kill "$du_pid" 2>/dev/null
      echo "du scan exceeded the ${du_budget_secs}s budget and was killed."
      if [ -f "$disk_cache" ]; then
        echo "Showing the last successful scan below — it may be stale, treat it as such."
      else
        echo "No prior cached scan exists either — CANNOT REPORT top dirs this run. Not the same as clean."
      fi
    else
      sort -rh "$tmp_out" > "$disk_cache"
      date +%s > "$disk_cache_meta"
    fi
    rm -f "$tmp_out"
  fi

  if [ -f "$disk_cache" ]; then
    cache_age_h=$(( ( $(date +%s) - $(cat "$disk_cache_meta" 2>/dev/null || echo 0) ) / 3600 ))
    echo "### Top 15 dirs by size (~/Projects + ~/Library, scan cached ${cache_age_h}h ago)"
    echo '```'
    head -15 "$disk_cache"
    echo '```'
  fi
  echo

  # Regenerable-cache sum: dev tooling caches that redownload/rebuild on
  # next use. Deliberately narrow and named — extend the list, don't guess.
  cache_paths=(
    "$HOME/.ollama/models"
    "$HOME/.cache/huggingface"
    "$HOME/.npm/_cacache"
    "$HOME/.npm/_npx"
    "$HOME/Library/Caches"
    "$HOME/Library/Application Support/Claude/Cache"
    "$HOME/Library/Application Support/Claude/Code Cache"
  )
  cache_kb=0
  for p in "${cache_paths[@]}"; do
    if [ -e "$p" ]; then
      k=$(du -xsk "$p" 2>/dev/null | awk '{print $1}')
      cache_kb=$(( cache_kb + ${k:-0} ))
    fi
  done
  cache_gb=$(awk -v kb="$cache_kb" 'BEGIN { printf "%.1f", kb / 1000000 }')
  echo "**Regenerable-cache sum** (ollama/huggingface/npm/system+Claude caches): ${cache_gb} GB"

  # .venv / node_modules sum across repos — bounded depth to stay in budget.
  venv_kb=0
  while IFS= read -r d; do
    k=$(du -xsk "$d" 2>/dev/null | awk '{print $1}')
    venv_kb=$(( venv_kb + ${k:-0} ))
  done < <(find "$HOME/Projects" -maxdepth 4 -type d \( -name ".venv" -o -name "venv" -o -name "node_modules" \) 2>/dev/null)
  venv_gb=$(awk -v kb="$venv_kb" 'BEGIN { printf "%.1f", kb / 1000000 }')
  echo "**.venv/node_modules sum across repos:** ${venv_gb} GB (regenerate with pip/npm install)"
  echo

  # Stale-worktree-scratch sum. Disposition of any single worktree is the
  # Worktree janitor dry run above (it cross-checks git state); this line is
  # a size total only, never a removal recommendation on its own.
  wt_paths=(
    "$HOME/Projects/_wt"
    "$HOME/Projects/.worktrees"
    "$HOME/Projects/lantern-wt"
    "$HOME/Projects/lantern-worktrees"
  )
  wt_kb=0
  for p in "${wt_paths[@]}"; do
    if [ -e "$p" ]; then
      k=$(du -xsk "$p" 2>/dev/null | awk '{print $1}')
      wt_kb=$(( wt_kb + ${k:-0} ))
    fi
  done
  wt_gb=$(awk -v kb="$wt_kb" 'BEGIN { printf "%.1f", kb / 1000000 }')
  echo "**Worktree-scratch sum** (_wt, .worktrees, lantern-wt*): ${wt_gb} GB — see Worktree janitor dry run above for per-worktree disposition, never delete from this total alone"
} >> "$report" || true

echo "$report"
