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

# OPS_HOME overrides every SOURCE path below (what gets backed up). OPS_VAULT
# and the two OPS_*REMOTE vars override the destinations. A test run points
# all four at a mktemp tree plus an isolated rclone config so it never touches
# the real ~/.claude, ~/ops-vault, or the real gdw-crypt: remote.
OPS_HOME="${OPS_HOME:-$HOME}"
VAULT="${OPS_VAULT:-$HOME/ops-vault}"
REMOTE="${OPS_REMOTE:-gdw-crypt:ops-vault}"
MEMORY_REMOTE="${OPS_MEMORY_REMOTE:-gdw-crypt:data-vaults/claude-memory}"
TS=$(date '+%Y-%m-%d %H:%M:%S')
# Second-resolution: a DATE-only backup-dir key collides across every push in
# the same day, so a second run's overwrite silently replaces the FIRST run's
# archived copy instead of adding a new one.
STAMP=$(date '+%Y-%m-%dT%H%M%S')
FAILTASK="$HOME/.claude/bin/failtask"

# OPS_NO_FAILTASK=1 suppresses the board-task filing only; the failure is
# still printed and the script still exits nonzero. A test drives a
# deliberate failure fixture and sets this so it never files a real task.
die() {
  echo "ops-snapshot: FAILED — $1"
  if [ -x "$FAILTASK" ] && [ "${OPS_NO_FAILTASK:-}" != "1" ]; then
    "$FAILTASK" infra "ops-snapshot failed: $1" \
      --detail "The ops-layer backup did not complete at $TS. ~/.claude has no other backup, so every hour this stays broken is unprotected. Log: ~/.claude/failures/ops-snapshot.log" \
      --dedupe-key ops-snapshot-failed --severity error >/dev/null 2>&1
  fi
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

sync_dir "$OPS_HOME/.claude/bin/"            "claude/bin/"
sync_dir "$OPS_HOME/.claude/hooks/"          "claude/hooks/"
sync_dir "$OPS_HOME/.claude/agents/"         "claude/agents/"
sync_dir "$OPS_HOME/.claude/agent-memory/"   "claude/agent-memory/"
sync_dir "$OPS_HOME/.claude/skills/"         "claude/skills/"
sync_dir "$OPS_HOME/.claude/commands/"       "claude/commands/"
sync_dir "$OPS_HOME/.claude/failures/"       "claude/failures/"
# links/: keep the source of truth (registry.json, render.py, covers, sessions.d, the gate
# pass) and skip GENERATED output. `site` is a symlink to a 219 MB built site and
# launchpad.html is a 5 MB generated artifact — with -L dereferencing symlinks, syncing the
# whole directory turned an 11 MB nightly into 457 MB. Both are re-derivable from
# registry.json + render.py, which is precisely why they don't belong in a backup.
sync_dir "$OPS_HOME/.claude/links/"          "claude/links/"   "site" "site/**" "launchpad.html"
sync_dir "$OPS_HOME/.claude/todos/"          "claude/todos/"
sync_dir "$OPS_HOME/.claude/bundles/"        "claude/bundles/"
sync_dir "$OPS_HOME/.claude/handoffs-archive/" "claude/handoffs-archive/"
sync_dir "$OPS_HOME/.claude/projects/-Users-jeffpinto/memory/" "claude/memory/"

# Added 2026-09-29: measured as present on the machine and absent from the
# vault. handoffs/ alone was 756K, and 65 of its 67 notes existed nowhere
# else. sync_dir already skips a missing directory quietly (its first line is
# `[ -e "$1" ] || return 0`), which is why these are safe to list even for a
# fresh machine that has not grown all of them yet.
sync_dir "$OPS_HOME/.claude/handoffs/"        "claude/handoffs/"
sync_dir "$OPS_HOME/.claude/observability/"   "claude/observability/"
sync_dir "$OPS_HOME/.claude/jobs/"            "claude/jobs/"
sync_dir "$OPS_HOME/.claude/scheduled/"       "claude/scheduled/"
sync_dir "$OPS_HOME/.claude/scheduled-tasks/" "claude/scheduled-tasks/"
sync_dir "$OPS_HOME/.claude/watch/"           "claude/watch/"
sync_dir "$OPS_HOME/.claude/state/"           "claude/state/"

# Repo-local agent memory. Personas write memory into whatever repo was the session cwd,
# and on 2026-07-24 84 of those 199 files were untracked — backed up NOWHERE, since this
# vault otherwise covers only ~/.claude. Mirrored here regardless of who eventually commits
# them, because "nothing is lost" outranks "filed in the right place". Includes
# codex-exchange's, which is correct: this vault is local + encrypted and never pushed.
for repo_dir in "$OPS_HOME"/Projects/*/; do
  mem="$repo_dir.claude/agent-memory"
  [ -d "$mem" ] || continue
  sync_dir "$mem/" "repos/$(basename "$repo_dir")/agent-memory/"
done

mkdir -p "$VAULT/claude" "$VAULT/LaunchAgents" "$VAULT/home" "$VAULT/home/local-bin"
cp -p "$OPS_HOME/.claude/CLAUDE.md"     "$VAULT/claude/CLAUDE.md"     2>/dev/null
cp -p "$OPS_HOME/.claude/settings.json" "$VAULT/claude/settings.json" 2>/dev/null
cp -p "$OPS_HOME/.gitignore_global"     "$VAULT/home/.gitignore_global" 2>/dev/null
rsync -a --delete --include 'com.jeff*.plist' --exclude '*' \
      "$OPS_HOME/Library/LaunchAgents/" "$VAULT/LaunchAgents/" || die "rsync plists"

# Added 2026-09-29: ~/.local/bin held three scripts (outbound-crm-serve.sh,
# site-dev-serve.sh, sweep-crm-import.py) with no offsite copy anywhere, plus
# a symlink named "claude" that must not be followed or copied. rsync with
# neither -l nor -L skips a symlink outright (it prints "skipping non-regular
# file") instead of dereferencing or preserving it, which is exactly "regular
# files only."
if [ -e "$OPS_HOME/.local/bin" ]; then
  rsync -a --no-links --delete --exclude '.DS_Store' \
        "$OPS_HOME/.local/bin/" "$VAULT/home/local-bin/" || die "rsync $OPS_HOME/.local/bin"
fi

# ---- restore doc ---------------------------------------------------------------
# Generated, never hand-written: a restore procedure that drifts from the script is worse
# than none. (The first hand-written copy was also lost when the vault was rebuilt.)
cat > "$VAULT/RESTORE.md" <<'DOC'
# Restoring the ops layer

Generated by `~/.claude/bin/ops-snapshot.sh` on every run — edit the script, not this file.

This vault mirrors the machine-local layer that runs the estate: `~/.claude` (scripts,
hooks, personas, agent memory, skills, failure history, todos, bundles, handoff archive,
handoffs, observability, jobs, scheduled, scheduled-tasks, watch, state, and the memory
dir), repo-local `.claude/agent-memory/` from every project, `~/.local/bin` (regular
files only, symlinks skipped), the `com.jeff*` launchd plists, and `~/.gitignore_global`.
Before 2026-07-24 none of it was backed up; the directories added 2026-09-29 had no
offsite copy of their own until then.

| Copy | Location | Survives |
|---|---|---|
| Working files | `~/.claude`, `~/Library/LaunchAgents`, `~/.local/bin` | nothing — the originals |
| Local history | `~/ops-vault` (git, branch `primary`, **no remote by design**) | a bad edit |
| Offsite | `gdw-crypt:ops-vault/current` (rclone, client-side encrypted) | laptop loss |

Deletions and overwrites are archived to `gdw-crypt:ops-vault/replaced/<stamp>` first, and
every push is verified file by file (`rclone check --one-way --size-only`), not just
counted. A `last-ok.json` at the root of this vault is written only after the push AND
that verify both succeed; a missing or stale one is the alarm condition.

A separate, much larger tree, `~/data-vaults/claude-memory` (memory-sync snapshots and
adjudication records), is backed up by this same script but is NOT part of this vault or
its git history. See "Restore the claude-memory archive" below.

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
   agent-memory, skills, commands, failures, links, todos, bundles, handoffs-archive,
   handoffs, observability, jobs, scheduled, scheduled-tasks, watch, state.
   Then `claude/memory/` -> `~/.claude/projects/-Users-jeffpinto/memory/`,
   `home/local-bin/` -> `~/.local/bin/` (the `claude` symlink there is not covered by
   this vault and is recreated by hand), and copy `claude/CLAUDE.md`,
   `claude/settings.json`, `home/.gitignore_global` back to `~`.
4. `cp ~/ops-vault/LaunchAgents/*.plist ~/Library/LaunchAgents/` then
   `for p in ~/Library/LaunchAgents/com.jeff*.plist; do launchctl bootstrap gui/$(id -u) "$p"; done`
   and diff `launchctl list | grep jeffpinto` against `LaunchAgents/` for anything unloaded.
5. `repos/<repo>/agent-memory/` holds persona memory that was untracked in project repos.
   Copy back only what its repo doesn't already track.
6. Restore separately, deliberately excluded here: the repos (GitHub), Claude credentials
   (Keychain, re-auth), `~/.cloudflared` tunnel credentials, and the rclone config itself.
7. Memory-sync archive: `rclone copy gdw-crypt:data-vaults/claude-memory/current
   ~/data-vaults/claude-memory`. This never went through this vault's git history or its
   rsync mirror; it is its own leg, pushed with `rclone copy` so nothing offsite is ever
   deleted by it, and can be turned off with `OPS_MEMORY_VAULT_OFFSITE=0`.

## Excluded on purpose

Transcripts (`~/.claude/projects/*` except `memory/`), audio-notes, uploads, file-history,
runlogs, worklog, plugins — bulky and replaceable. Also `links/site` and
`links/launchpad.html`: generated output, re-derivable from `registry.json` + `render.py`.
Including them once turned an 11 MB nightly into 457 MB. `~/data-vaults/claude-memory` is
not excluded; it is covered by the separate leg in step 7 above, not by this vault's mirror.

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
# Re-checked on EVERY run that the remote is genuinely type=crypt, never assumed
# from its name: a remote can be reconfigured out from under a label, and this
# vault holds client-sensitive agent memory in plaintext until it leaves the
# machine. Added 2026-09-29; the production script had never had this check.
rtype=$(rclone config show "${REMOTE%%:*}" 2>/dev/null | sed -n 's/^type = //p')
[ "$rtype" = "crypt" ] || die "refusing offsite: remote ${REMOTE%%:*} is type '${rtype:-unknown}', not crypt"

# Preflight before the push, short timeout, so a dead OAuth token or a dead
# network is named distinctly instead of falling through to the same
# "0 objects" message a genuinely empty push would also produce. rc=3 is
# rclone's own "directory not found" exit code, which is what a remote PATH
# that has never been pushed to returns on its first-ever run -- reachable
# and authenticated, just empty so far. Only OTHER nonzero codes are a real
# auth/reachability failure. Found by local-tools/tests/test_ops_snapshot.sh:
# the claude-memory leg below pushes to a remote path with no prior history,
# so without this its very first production run would die here every time.
preflight_err=$(rclone lsd --max-depth 1 "$REMOTE" --timeout 20s --contimeout 10s 2>&1 >/dev/null)
preflight_rc=$?
if [ "$preflight_rc" -ne 0 ] && [ "$preflight_rc" -ne 3 ]; then
  die "offsite preflight failed for $REMOTE (auth or reachability); rclone said: $preflight_err"
fi

# rc captured directly from rclone, never through a pipe. `cmd | tail -3` in an
# `if` tests tail's exit status, not rclone's, so a failed push still printed
# "pushed". Found in adversarial review 2026-09-29; left unfixed nowhere else
# on purpose, because this script did not exist anywhere but ~/.claude/bin
# until this change, so there was nowhere else to fix it.
sync_out=$(rclone sync "$VAULT" "$REMOTE/current" \
      --backup-dir "$REMOTE/replaced/$STAMP" \
      --exclude '.DS_Store' --transfers 4 --timeout 5m 2>&1)
sync_rc=$?
printf '%s\n' "$sync_out" | tail -3
[ "$sync_rc" -eq 0 ] || die "rclone sync to $REMOTE (rc=$sync_rc)"
echo "ops-snapshot: pushed to $REMOTE/current at $TS"

# Verify per file, not "more than 100 objects": an object count only proves
# SOME push landed on SOME past night, never that tonight's push landed and
# matches. --one-way so files that exist offsite only under replaced/<stamp>
# history are not reported as differences.
check_out=$(rclone check --one-way --size-only "$VAULT" "$REMOTE/current" 2>&1)
check_rc=$?
printf '%s\n' "$check_out" | tail -5
[ "$check_rc" -eq 0 ] || die "rclone check found differences between $VAULT and $REMOTE/current (rc=$check_rc)"
echo "ops-snapshot: verified $VAULT matches $REMOTE/current at $TS"

# Freshness marker, written LAST and only once the push AND the per-file
# verify have both succeeded. It lands inside $VAULT, so it rides along in
# the next run's commit and push rather than needing its own write path.
printf '{"last_ok":"%s","remote":"%s"}\n' "$(date -Iseconds)" "$REMOTE/current" > "$VAULT/last-ok.json" \
  || die "cannot write last-ok.json"

# ---- claude-memory offsite (separate leg, not part of this vault's git tree) ---
# ~/data-vaults/claude-memory (about 1.4 GB of memory-sync snapshots and
# adjudication records) had no offsite copy of any kind as of 2026-09-29.
# rclone COPY, never sync: this leg must never delete anything offsite, since
# it does not own rotation for that tree the way the notes-vault/lantern/etc
# snapshot scripts own rotation for theirs.
if [ "${OPS_MEMORY_VAULT_OFFSITE:-1}" = "1" ]; then
  MEMORY_SRC="$OPS_HOME/data-vaults/claude-memory"
  if [ -e "$MEMORY_SRC" ]; then
    mrtype=$(rclone config show "${MEMORY_REMOTE%%:*}" 2>/dev/null | sed -n 's/^type = //p')
    [ "$mrtype" = "crypt" ] || die "refusing claude-memory offsite: remote ${MEMORY_REMOTE%%:*} is type '${mrtype:-unknown}', not crypt"

    mpre_err=$(rclone lsd --max-depth 1 "$MEMORY_REMOTE" --timeout 20s --contimeout 10s 2>&1 >/dev/null)
    mpre_rc=$?
    if [ "$mpre_rc" -ne 0 ] && [ "$mpre_rc" -ne 3 ]; then
      die "claude-memory offsite preflight failed for $MEMORY_REMOTE (auth or reachability); rclone said: $mpre_err"
    fi

    mcopy_out=$(rclone copy "$MEMORY_SRC" "$MEMORY_REMOTE/current" \
          --backup-dir "$MEMORY_REMOTE/replaced/$STAMP" \
          --exclude '.DS_Store' --transfers 4 --timeout 30m 2>&1)
    mcopy_rc=$?
    printf '%s\n' "$mcopy_out" | tail -3
    [ "$mcopy_rc" -eq 0 ] || die "rclone copy of claude-memory to $MEMORY_REMOTE (rc=$mcopy_rc)"
    echo "ops-snapshot: pushed claude-memory to $MEMORY_REMOTE/current at $TS"

    mcheck_out=$(rclone check --one-way --size-only "$MEMORY_SRC" "$MEMORY_REMOTE/current" 2>&1)
    mcheck_rc=$?
    printf '%s\n' "$mcheck_out" | tail -5
    [ "$mcheck_rc" -eq 0 ] || die "rclone check found differences between $MEMORY_SRC and $MEMORY_REMOTE/current (rc=$mcheck_rc)"
    echo "ops-snapshot: verified $MEMORY_SRC matches $MEMORY_REMOTE/current at $TS"
  else
    echo "ops-snapshot: claude-memory offsite skipped, no $MEMORY_SRC"
  fi
else
  echo "ops-snapshot: claude-memory offsite disabled (OPS_MEMORY_VAULT_OFFSITE=0)"
fi
