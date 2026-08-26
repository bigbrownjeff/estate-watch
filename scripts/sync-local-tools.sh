#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mode="${1:---check}"

sources=(
  "$repo_root/local-tools/projects-hygiene.sh"
  "$repo_root/scripts/estate_drift.py"
  "$repo_root/config/estate-drift.json"
  "$repo_root/local-tools/skills/ship-pr/SKILL.md"
  "$repo_root/local-tools/memory-sync"
)
targets=(
  "$HOME/.claude/bin/projects-hygiene.sh"
  "$HOME/.claude/bin/estate-drift.py"
  "$HOME/.claude/config/estate-drift.json"
  "$HOME/.claude/skills/ship-pr/SKILL.md"
  "$HOME/.claude/bin/memory-sync"
)

case "$mode" in
  --check)
    for i in "${!sources[@]}"; do
      if [[ ! -f "${targets[$i]}" ]] || ! cmp -s "${sources[$i]}" "${targets[$i]}"; then
        echo "stale or missing install: ${targets[$i]}" >&2
        exit 1
      fi
    done
    echo "estate-watch local tools are current"
    ;;
  --install)
    mkdir -p "$HOME/.claude/bin" "$HOME/.claude/config"
    mkdir -p "$HOME/.claude/skills/ship-pr"
    install -m 0755 "${sources[0]}" "${targets[0]}"
    install -m 0755 "${sources[1]}" "${targets[1]}"
    install -m 0644 "${sources[2]}" "${targets[2]}"
    install -m 0644 "${sources[3]}" "${targets[3]}"
    install -m 0755 "${sources[4]}" "${targets[4]}"
    "$0" --check
    ;;
  *)
    echo "usage: $0 [--check|--install]" >&2
    exit 2
    ;;
esac
