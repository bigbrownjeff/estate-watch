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

echo "$report"
