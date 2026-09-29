#!/bin/bash
# ops-snapshot — version and back up the layer that runs the estate.
#
# Why: as of 2026-07-24 there was NO backup of ~/.claude — no Time Machine destination, no
# repo, nothing. estate-pulse, fleet-sentinel, failtask, failack, sqlite-lint, every persona,
# every agent memory and 33 launchd plists were a single unbacked copy on one laptop. The
# thing that watches the estate was the least protected thing in it.
#
# Design (Jeff picked "option E", 2026-07-24):
#   1. LOCAL git vault at ~/ops-vault — history for the common case ("I broke estate-pulse.py").
#      Deliberately NOT under ~/Projects, where an agent would eventually push it. A pre-push
#      hook refuses pushes outright.
#   2. ENCRYPTED offsite via rclone's gdw-crypt: remote — survives laptop loss. rclone crypt
#      encrypts client-side, so nothing plaintext ever leaves. That matters: the ops tree holds
#      client-sensitive agent memory and a failure history full of client names, which is
#      exactly why a plaintext GitHub mirror (and its permanent scrub gate) was rejected.
#
# Deletions are never destructive: rclone sync writes removals to a dated backup-dir.
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin"

VAULT="$HOME/ops-vault"
REMOTE="gdw-crypt:ops-vault"
TS=$(date '+%Y-%m-%d %H:%M:%S')
DATE=$(date '+%Y-%m-%d')
FAILTASK="$HOME/.claude/bin/failtask"

die() {
  echo "ops-snapshot: FAILED — $1"
  [ -x "$FAILTASK" ] && "$FAILTASK" infra "ops-snapshot failed: $1" \
    --detail "The ops-layer backup did not complete at $TS. ~/.claude has no other backup, so every hour this stays broken is unprotected. Log: ~/.claude/failures/ops-snapshot.log" \
    --dedupe-key ops-snapshot-failed --severity error >/dev/null 2>&1
  exit 1
}

mkdir -p "$VAULT" || die "cannot create $VAULT"

# ---- one-time init -------------------------------------------------------------
if [ ! -d "$VAULT/.git" ]; then
  git -C "$VAULT" init -q || die "git init failed"
  git -C "$VAULT" symbolic-ref HEAD refs/heads/primary
  mkdir -p "$VAULT/.git/hooks"
  cat > "$VAULT/.git/hooks/pre-push" <<'HOOK'
#!/bin/sh
echo "ops-vault: push REFUSED. This vault holds client-sensitive agent memory and failure"
echo "history in plaintext. Its offsite copy is the encrypted rclone remote (gdw-crypt:),"
echo "not a git remote. If you genuinely need one, encrypt first and change this hook."
exit 1
HOOK
  chmod +x "$VAULT/.git/hooks/pre-push"
fi

# ---- mirror the allowlist ------------------------------------------------------
# Explicit allowlist, never a denylist: ~/.claude is 4+ GB, of which the part whose loss
# actually hurts is ~11 MB. Transcripts, audio notes, uploads, file-history, runlogs and
# worklog are deliberately excluded — bulky and replaceable.
# -L dereferences symlinks into real files. Without it the first run stored 16 dangling
# links (8 personas in agents/, 8 in skills/, all pointing into ~/Projects/personas/*/dist/)
# and rclone skipped every one — "Can't follow symlink without -L". The persona definitions
# would have been absent from the offsite copy while appearing present in the vault.
sync_dir() {  # $1 source  $2 dest-relative  $3.. extra --exclude patterns
  [ -e "$1" ] || return 0
  local src="$1" dst="$2"; shift 2
  local ex=(); for p in "$@"; do ex+=(--exclude "$p"); done
  mkdir -p "$(dirname "$VAULT/$dst")"
  # ${ex[@]+"${ex[@]}"} not "${ex[@]}": macOS /bin/bash is 3.2, where expanding an EMPTY
  # array under `set -u` aborts with "ex[@]: unbound variable". launchd runs /bin/bash
  # explicitly while an interactive test picks up homebrew bash 5, which tolerates it — so
  # this passed by hand and failed on its first 02:30 run (2026-07-25), skipping a backup.
  rsync -aL --delete --exclude '.DS_Store' ${ex[@]+"${ex[@]}"} "$src" "$VAULT/$dst" \
    || die "rsync $src"
}

sync_dir "$HOME/.claude/bin/"            "claude/bin/"
sync_dir "$HOME/.claude/hooks/"          "claude/hooks/"
sync_dir "$HOME/.claude/agents/"         "claude/agents/"
sync_dir "$HOME/.claude/agent-memory/"   "claude/agent-memory/"
sync_dir "$HOME/.claude/skills/"         "claude/skills/"
sync_dir "$HOME/.claude/commands/"       "claude/commands/"
sync_dir "$HOME/.claude/failures/"       "claude/failures/"
# links/: keep the source of truth (registry.json, render.py, covers, sessions.d, the gate
# pass) and skip GENERATED output. `site` is a symlink to a 219 MB built site and
# launchpad.html is a 5 MB generated artifact — with -L dereferencing symlinks, syncing the
# whole directory turned an 11 MB nightly into 457 MB. Both are re-derivable from
# registry.json + render.py, which is precisely why they don't belong in a backup.
sync_dir "$HOME/.claude/links/"          "claude/links/"   "site" "site/**" "launchpad.html"
sync_dir "$HOME/.claude/todos/"          "claude/todos/"
sync_dir "$HOME/.claude/bundles/"        "claude/bundles/"
sync_dir "$HOME/.claude/handoffs-archive/" "claude/handoffs-archive/"
sync_dir "$HOME/.claude/projects/-Users-jeffpinto/memory/" "claude/memory/"

# Repo-local agent memory. Personas write memory into whatever repo was the session cwd,
# and on 2026-07-24 84 of those 199 files were untracked — backed up NOWHERE, since this
# vault otherwise covers only ~/.claude. Mirrored here regardless of who eventually commits
# them, because "nothing is lost" outranks "filed in the right place". Includes
# codex-exchange's, which is correct: this vault is local + encrypted and never pushed.
for repo_dir in "$HOME"/Projects/*/; do
  mem="$repo_dir.claude/agent-memory"
  [ -d "$mem" ] || continue
  sync_dir "$mem/" "repos/$(basename "$repo_dir")/agent-memory/"
done

mkdir -p "$VAULT/claude" "$VAULT/LaunchAgents" "$VAULT/home"
cp -p "$HOME/.claude/CLAUDE.md"     "$VAULT/claude/CLAUDE.md"     2>/dev/null
cp -p "$HOME/.claude/settings.json" "$VAULT/claude/settings.json" 2>/dev/null
cp -p "$HOME/.gitignore_global"     "$VAULT/home/.gitignore_global" 2>/dev/null
rsync -a --delete --include 'com.jeff*.plist' --exclude '*' \
      "$HOME/Library/LaunchAgents/" "$VAULT/LaunchAgents/" || die "rsync plists"

# ---- restore doc ---------------------------------------------------------------
# Generated, never hand-written: a restore procedure that drifts from the script is worse
# than none. (The first hand-written copy was also lost when the vault was rebuilt.)
cat > "$VAULT/RESTORE.md" <<'DOC'
# Restoring the ops layer

Generated by `~/.claude/bin/ops-snapshot.sh` on every run — edit the script, not this file.

This vault mirrors the machine-local layer that runs the estate: `~/.claude` (scripts,
hooks, personas, agent memory, skills, failure history, todos, bundles, handoff archive,
the memory dir), repo-local `.claude/agent-memory/` from every project, the `com.jeff*`
launchd plists, and `~/.gitignore_global`. Before 2026-07-24 none of it was backed up.

| Copy | Location | Survives |
|---|---|---|
| Working files | `~/.claude`, `~/Library/LaunchAgents` | nothing — the originals |
| Local history | `~/ops-vault` (git, branch `primary`, **no remote by design**) | a bad edit |
| Offsite | `gdw-crypt:ops-vault/current` (rclone, client-side encrypted) | laptop loss |

Deletions and overwrites are archived to `gdw-crypt:ops-vault/replaced/<date>` first.

## Roll back one bad edit

    cd ~/ops-vault
    git log --oneline -- claude/bin/estate-pulse.py
    git show <sha>:claude/bin/estate-pulse.py > ~/.claude/bin/estate-pulse.py

## Rebuild on a new Mac

1. Install rclone, restore its config for the `gdw` / `gdw-crypt` remotes.
   **Without the crypt password + salt the offsite copy is unreadable.** They live in
   `~/.config/rclone/rclone.conf` under `[gdw-crypt]` as `password` / `password2`, only
   *obscured* (reversible), so keep a copy of that file in the password manager — NOT in
   this vault, which it decrypts.
2. `rclone copy gdw-crypt:ops-vault/current ~/ops-vault`
3. `rsync -a ~/ops-vault/claude/<dir>/ ~/.claude/<dir>/` for each of: bin, hooks, agents,
   agent-memory, skills, commands, failures, links, todos, bundles, handoffs-archive.
   Then `claude/memory/` -> `~/.claude/projects/-Users-jeffpinto/memory/`, and copy
   `claude/CLAUDE.md`, `claude/settings.json`, `home/.gitignore_global` back to `~`.
4. `cp ~/ops-vault/LaunchAgents/*.plist ~/Library/LaunchAgents/` then
   `for p in ~/Library/LaunchAgents/com.jeff*.plist; do launchctl bootstrap gui/$(id -u) "$p"; done`
   and diff `launchctl list | grep jeffpinto` against `LaunchAgents/` for anything unloaded.
5. `repos/<repo>/agent-memory/` holds persona memory that was untracked in project repos.
   Copy back only what its repo doesn't already track.
6. Restore separately, deliberately excluded here: the repos (GitHub), Claude credentials
   (Keychain, re-auth), `~/.cloudflared` tunnel credentials, and the rclone config itself.

## Excluded on purpose

Transcripts (`~/.claude/projects/*` except `memory/`), audio-notes, uploads, file-history,
runlogs, worklog, plugins — bulky and replaceable. Also `links/site` and
`links/launchpad.html`: generated output, re-derivable from `registry.json` + `render.py`.
Including them once turned an 11 MB nightly into 457 MB.

## Why no GitHub mirror

The tree holds client-sensitive agent memory and a failure history full of client names.
A plaintext mirror needs a scrub gate maintained forever, and a scrub gate is what failed
on 2026-07-16. rclone crypt encrypts client-side, so nothing plaintext leaves and there is
no gate to maintain. A `pre-push` hook refuses git pushes for the same reason.
DOC

# ---- commit --------------------------------------------------------------------
cd "$VAULT" || die "cd $VAULT"
git add -A || die "git add"
if git diff --cached --quiet; then
  echo "ops-snapshot: no changes at $TS"
else
  n=$(git diff --cached --name-only | wc -l | tr -d ' ')
  git -c user.name="ops-snapshot" -c user.email="ops@localhost" \
      commit -q -m "snapshot $TS ($n file(s))" || die "git commit"
  echo "ops-snapshot: committed $n changed file(s)"
fi

# ---- encrypted offsite ---------------------------------------------------------
# --backup-dir means a deletion or overwrite is archived, never lost outright.
if rclone sync "$VAULT" "$REMOTE/current" \
      --backup-dir "$REMOTE/replaced/$DATE" \
      --exclude '.DS_Store' --transfers 4 --timeout 5m 2>&1 | tail -3; then
  echo "ops-snapshot: pushed to $REMOTE/current at $TS"
else
  die "rclone sync to $REMOTE"
fi

# Prove the copy is real rather than assuming a zero exit means data landed.
remote_n=$(rclone size "$REMOTE/current" --json 2>/dev/null | sed -n 's/.*"count":\([0-9]*\).*/\1/p')
[ -n "$remote_n" ] && [ "$remote_n" -gt 100 ] || die "remote verify: only ${remote_n:-0} objects"
echo "ops-snapshot: verified $remote_n objects offsite at $TS"
