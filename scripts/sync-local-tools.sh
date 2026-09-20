#!/usr/bin/env bash
set -euo pipefail

# Versioned sources for the tools installed under ~/.claude. Every row is
# "<repo path>|<install target>|<mode>". Add a row the day a tool lands in
# ~/.claude/bin; a tool that lives only there is uncommitted anywhere
# (2026-09-09: four watchdog fixes existed only on one disk until this table
# grew). `--check` fails loudly on any drift in either direction; the weekly
# projects-hygiene run calls it.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mode="${1:---check}"

rows=(
  "local-tools/projects-hygiene.sh|$HOME/.claude/bin/projects-hygiene.sh|0755"
  "scripts/estate_drift.py|$HOME/.claude/bin/estate-drift.py|0755"
  "config/estate-drift.json|$HOME/.claude/config/estate-drift.json|0644"
  "local-tools/skills/ship-pr/SKILL.md|$HOME/.claude/skills/ship-pr/SKILL.md|0644"
  "local-tools/memory-sync|$HOME/.claude/bin/memory-sync|0755"
  "local-tools/memory-index-rebuild|$HOME/.claude/bin/memory-index-rebuild|0755"
  "local-tools/codex-memory-import|$HOME/.claude/bin/codex-memory-import|0755"
  "local-tools/gh-board-export.sh|$HOME/.claude/bin/gh-board-export.sh|0755"
  "local-tools/board-ref|$HOME/.claude/bin/board-ref|0755"
  "local-tools/failtask|$HOME/.claude/bin/failtask|0755"
  "local-tools/tests/test_failtask_issue_path.py|$HOME/.claude/bin/tests/test_failtask_issue_path.py|0755"
  "local-tools/tests/test_failtask_age_escalation.py|$HOME/.claude/bin/tests/test_failtask_age_escalation.py|0755"
  "local-tools/tests/test_gh_board_export_comments.sh|$HOME/.claude/bin/tests/test_gh_board_export_comments.sh|0755"
  "local-tools/burn-meter.py|$HOME/.claude/bin/burn-meter.py|0755"
  "local-tools/hooks/turn-cap-gate.py|$HOME/.claude/hooks/turn-cap-gate.py|0755"
  "local-tools/tests/test_turn_cap_gate.py|$HOME/.claude/bin/tests/test_turn_cap_gate.py|0755"
  "local-tools/tests/test_burn_meter_dedupe.py|$HOME/.claude/bin/tests/test_burn_meter_dedupe.py|0755"
  "local-tools/heal-sweep.py|$HOME/.claude/bin/heal-sweep.py|0755"
  "local-tools/tests/test_heal_sweep_burn.py|$HOME/.claude/bin/tests/test_heal_sweep_burn.py|0755"
  "local-tools/phoenix-enrich.py|$HOME/.claude/bin/phoenix-enrich.py|0755"
  "local-tools/morning-digest.py|$HOME/.claude/bin/morning-digest.py|0755"
  "local-tools/worktree-janitor.py|$HOME/.claude/bin/worktree-janitor.py|0755"
  "local-tools/dangling-tasks.py|$HOME/.claude/bin/dangling-tasks.py|0755"
  "local-tools/prompt-maestro-weekly.sh|$HOME/.claude/bin/prompt-maestro-weekly.sh|0755"
  "local-tools/tests/test_prompt_maestro_weekly.sh|$HOME/.claude/bin/tests/test_prompt_maestro_weekly.sh|0755"
  "local-tools/log-rotate.py|$HOME/.claude/bin/log-rotate.py|0755"
  "local-tools/tests/test_log_rotate.py|$HOME/.claude/bin/tests/test_log_rotate.py|0755"
)

case "$mode" in
  --check)
    rc=0
    for row in "${rows[@]}"; do
      IFS='|' read -r src dst _ <<<"$row"
      if [[ ! -f "$repo_root/$src" ]]; then
        echo "missing versioned source: $src" >&2; rc=1
      elif [[ ! -f "$dst" ]] || ! cmp -s "$repo_root/$src" "$dst"; then
        echo "stale or missing install: $dst (source $src)" >&2; rc=1
      fi
    done
    [[ $rc -eq 0 ]] && echo "estate-watch local tools are current (${#rows[@]} tools)"
    exit $rc
    ;;
  --install)
    for row in "${rows[@]}"; do
      IFS='|' read -r src dst perm <<<"$row"
      mkdir -p "$(dirname "$dst")"
      install -m "$perm" "$repo_root/$src" "$dst"
    done
    "$0" --check
    ;;
  *)
    echo "usage: $0 [--check|--install]" >&2
    exit 2
    ;;
esac
