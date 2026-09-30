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
#
# Secrets and audio-notes offsite (Jeff's ruling, 2026-09-30, verbatim "API backup yes.
# Call backup yes."): both trees were previously excluded on purpose (see the generated
# RESTORE.md's own "Excluded on purpose" section, before this change). They now get their
# own independent, encrypted, never-through-the-plaintext-vault legs (estate-secrets,
# audio-notes below) -- staged secrets never enter $VAULT or its git history. The one
# standing exclusion is Projects/mattel-engagement: a client repo whose local overlay
# stays off any offsite copy.
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
# Freshness marker for the ops-vault leg, kept OUTSIDE the git-tracked $VAULT tree on
# purpose: a marker written inside $VAULT changes on every run (the timestamp), so git
# always has something to commit and "ops-snapshot: no changes" can never print again.
OPS_MARKER="${OPS_MARKER:-$HOME/data-vaults/ops-vault/last-ok.json}"
# Same filter used by BOTH the push and the check for a given leg, from one variable, so
# a .DS_Store the push already skips can never be reported "missing offsite" by the check
# that is supposed to verify that exact push.
MAIN_FILTER=(--exclude '.DS_Store')
MEM_FILTER=(--exclude '.DS_Store')
# estate-secrets: API keys, tunnel credentials, rclone/wrangler configs and per-repo
# .env/.dev.vars files, staged fresh every run into a mode-700 mktemp dir under $OPS_HOME
# (never /tmp, never $VAULT) and pushed encrypted. Never through the plaintext ops-vault.
OPS_SECRETS_OFFSITE="${OPS_SECRETS_OFFSITE:-1}"
OPS_SECRETS_REMOTE="${OPS_SECRETS_REMOTE:-gdw-crypt:estate-secrets}"
OPS_SECRETS_MARKER="${OPS_SECRETS_MARKER:-$OPS_HOME/data-vaults/estate-secrets-offsite/last-ok.json}"
SECRETS_FILTER=(--exclude '.DS_Store')
# audio-notes: 2.3G of call recordings, ~1.8G after excluding the replaceable whisper
# models/ subdir. rclone COPY, never sync -- this leg does not own rotation for this tree.
OPS_AUDIO_OFFSITE="${OPS_AUDIO_OFFSITE:-1}"
OPS_AUDIO_REMOTE="${OPS_AUDIO_REMOTE:-gdw-crypt:data-vaults/audio-notes}"
OPS_AUDIO_MARKER="${OPS_AUDIO_MARKER:-$OPS_HOME/data-vaults/audio-notes-offsite/last-ok.json}"
AUDIO_FILTER=(--exclude 'models/**' --exclude '.DS_Store')
TS=$(date '+%Y-%m-%d %H:%M:%S')
# Second-resolution: a DATE-only backup-dir key collides across every push in
# the same day, so a second run's overwrite silently replaces the FIRST run's
# archived copy instead of adding a new one.
STAMP=$(date '+%Y-%m-%dT%H%M%S')
FAILTASK="$HOME/.claude/bin/failtask"
# Independent-leg failure count (S3): the ops-vault and claude-memory legs still hard-stop
# on their own failure via die() (unchanged, see the S3 note in the die()/fail_leg block
# below), but estate-secrets and audio-notes are new, mutually independent legs -- one
# failing must never block the other, or stop the script before the marker files for a
# leg that DID succeed get written. LEGS_FAILED is what makes the script still exit
# nonzero at the very end if either of them failed, even though neither calls exit itself.
LEGS_FAILED=0

# OPS_NO_FAILTASK=1 suppresses the board-task filing only; the failure is
# still printed and the script still exits nonzero. A test drives a
# deliberate failure fixture and sets this so it never files a real task.
#
# $2 (default "ops") picks the board card's title/dedupe-key/detail. Each leg is a
# separate lane on purpose: it must file its own card under its own dedupe key, never
# reuse or overwrite another leg's card. Only the ops-vault leg touches $OPS_MARKER, and
# only after its own push+check succeed; this function never does.
#
# S3 split fail_leg (files the task, prints, RETURNS 1 -- never exits) from die() (the
# same, then exit 1). Design decision, flagged per the spec: ops-vault and claude-memory
# keep today's hard-stop behavior unchanged (both still call die()) -- ops-vault because
# nothing downstream can succeed if $VAULT itself can't be created or committed to, and
# claude-memory because that lane's control flow was not part of this change's scope
# (only its dispatch text moved from if/else into this case). estate-secrets and
# audio-notes are the two NEW, mutually independent legs S3 requires: they call fail_leg
# and return, so a secrets failure never stops the audio leg (or vice versa), and
# LEGS_FAILED (set once, near the top of the script) is what still exits the whole script
# nonzero at the end if either of them failed.
fail_leg() {
  local msg="$1" lane="${2:-ops}" title dedupe detail
  case "$lane" in
    claude-memory)
      title="claude-memory offsite failed: $msg"
      dedupe="claude-memory-offsite-failed"
      detail="The claude-memory offsite backup did not complete at $TS. ~/data-vaults/claude-memory has no offsite copy of its own until this leg succeeds again. Log: ~/.claude/failures/ops-snapshot.log"
      ;;
    estate-secrets)
      title="estate-secrets offsite failed: $msg"
      dedupe="estate-secrets-offsite-failed"
      detail="The estate-secrets offsite backup did not complete at $TS. API keys, tunnel credentials, rclone/wrangler configs and per-repo .env/.dev.vars files have no offsite copy of their own until this leg succeeds again. Log: ~/.claude/failures/ops-snapshot.log"
      ;;
    audio-notes)
      title="audio-notes offsite failed: $msg"
      dedupe="audio-notes-offsite-failed"
      detail="The audio-notes offsite backup did not complete at $TS. ~/.claude/audio-notes has no offsite copy of its own until this leg succeeds again. Log: ~/.claude/failures/ops-snapshot.log"
      ;;
    *)
      title="ops-snapshot failed: $msg"
      dedupe="ops-snapshot-failed"
      detail="The ops-layer backup did not complete at $TS. ~/.claude has no other backup, so every hour this stays broken is unprotected. Log: ~/.claude/failures/ops-snapshot.log"
      ;;
  esac
  echo "ops-snapshot: FAILED — $msg"
  if [ -x "$FAILTASK" ] && [ "${OPS_NO_FAILTASK:-}" != "1" ]; then
    "$FAILTASK" infra "$title" --detail "$detail" \
      --dedupe-key "$dedupe" --severity error >/dev/null 2>&1
  fi
  return 1
}

die() {
  fail_leg "$@"
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
  # --no-links makes rsync print "skipping non-regular file" for every symlink it walks
  # past (the "claude" symlink, by design). That notice is expected on every run, not an
  # error, so it is filtered rather than left to read as one in the nightly log.
  localbin_out=$(rsync -a --no-links --delete --exclude '.DS_Store' \
        "$OPS_HOME/.local/bin/" "$VAULT/home/local-bin/" 2>&1)
  localbin_rc=$?
  localbin_filtered=$(printf '%s\n' "$localbin_out" | grep -v 'skipping non-regular file')
  [ -n "$localbin_filtered" ] && printf '%s\n' "$localbin_filtered"
  [ "$localbin_rc" -eq 0 ] || die "rsync $OPS_HOME/.local/bin"
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
| Secrets offsite | `gdw-crypt:estate-secrets/current` (rclone, client-side encrypted) | laptop loss |
| Audio offsite | `gdw-crypt:data-vaults/audio-notes/current` (rclone, client-side encrypted) | laptop loss |

Deletions and overwrites are archived to `gdw-crypt:ops-vault/replaced/<stamp>` first, and
every push is verified file by file (`rclone check --one-way --size-only`), not just
counted. A `last-ok.json` (default `~/data-vaults/ops-vault/last-ok.json`, override with
OPS_MARKER) is written only after the push AND that verify both succeed; it lives OUTSIDE
this vault's git tree on purpose, so a quiet night still shows as "no changes" here. A
missing or stale one is the alarm condition.

A separate, much larger tree, `~/data-vaults/claude-memory` (memory-sync snapshots and
adjudication records), is backed up by this same script but is NOT part of this vault or
its git history. See "Restore the claude-memory archive" below. It has its own freshness
marker (default `~/data-vaults/claude-memory-offsite/last-ok.json`, override with
OPS_MEM_MARKER) and its own failure card, separate from the ops-vault leg above. A
`last-ok.json` memory-sync itself writes inside `~/data-vaults/claude-memory` belongs to
memory-sync, not this leg, and is simply backed up like any other file in that tree.

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
   deleted by it, and can be turned off with `OPS_MEMORY_VAULT_OFFSITE=0`. Its freshness
   marker lives at `~/data-vaults/claude-memory-offsite/last-ok.json`, a directory of its
   own, never inside `~/data-vaults/claude-memory` itself.

## Restore the estate-secrets offsite copy

`rclone copy gdw-crypt:estate-secrets/current ~/estate-secrets-restore`, then place each
file back at its original relative path under `~` (the staged tree mirrors `~` exactly:
`.claude/secrets/...`, `.cloudflared/...`, `.config/rclone/rclone.conf`,
`.wrangler/config/default.toml`, `Library/Preferences/.wrangler/config/default.toml`, and
each project's `.env`/`.dev.vars` under `Projects/<repo>/...`). rclone does not reliably
round-trip original Unix modes through a crypt remote the way local `rsync -a` does inside
`~/ops-vault`, so after copying each file back, set its mode explicitly:
`chmod 600 <restored-file>`. Never restore into a git-tracked tree. Turned off with
`OPS_SECRETS_OFFSITE=0`. Freshness marker at
`~/data-vaults/estate-secrets-offsite/last-ok.json` (override with `OPS_SECRETS_MARKER`).

## Restore the audio-notes offsite copy

`rclone copy gdw-crypt:data-vaults/audio-notes/current ~/.claude/audio-notes`. Pushed with
`rclone copy`, so nothing offsite is ever deleted by this leg and the local `models/`
subdir (excluded from the push, replaceable whisper models) is untouched by a restore too
— reinstall those separately. Turned off with `OPS_AUDIO_OFFSITE=0`. Freshness marker at
`~/data-vaults/audio-notes-offsite/last-ok.json` (override with `OPS_AUDIO_MARKER`).

## Excluded on purpose

Transcripts (`~/.claude/projects/*` except `memory/`), uploads, file-history, runlogs,
worklog, plugins — bulky and replaceable. Also `links/site` and `links/launchpad.html`:
generated output, re-derivable from `registry.json` + `render.py`. Including them once
turned an 11 MB nightly into 457 MB. `~/data-vaults/claude-memory` is not excluded; it is
covered by the separate leg in step 7 above, not by this vault's mirror. `audio-notes` is
no longer excluded either (2026-09-30 ruling) — only its `models/` subdirectory is, same
pattern as `claude-memory`: covered by its own leg above, not by this vault's mirror.
Secrets are likewise covered by their own leg, never by this vault's mirror. The one
standing exclusion is `Projects/mattel-engagement`: a client repo whose local overlay
stays off any offsite copy, including the estate-secrets leg.

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
      "${MAIN_FILTER[@]}" --transfers 4 --timeout 5m 2>&1)
sync_rc=$?
[ -n "$sync_out" ] && printf '%s\n' "$sync_out" | tail -3
[ "$sync_rc" -eq 0 ] || die "rclone sync to $REMOTE (rc=$sync_rc)"
echo "ops-snapshot: pushed to $REMOTE/current at $TS"

# Verify per file, not "more than 100 objects": an object count only proves
# SOME push landed on SOME past night, never that tonight's push landed and
# matches. --one-way so files that exist offsite only under replaced/<stamp>
# history are not reported as differences. Same filter as the push above
# (MAIN_FILTER), so a .DS_Store the push already skipped can never show up
# here as a difference.
check_out=$(rclone check --one-way --size-only "${MAIN_FILTER[@]}" "$VAULT" "$REMOTE/current" 2>&1)
check_rc=$?
# Print rclone's own error/notice lines only when the check actually failed; a
# passing check is silent here (nothing to guard with [ -n ] on success, since
# nothing is printed at all). "No common hash found" is rclone's expected
# NOTICE for --size-only, not a problem, so it is filtered even on failure.
if [ "$check_rc" -ne 0 ]; then
  check_filtered=$(printf '%s\n' "$check_out" | grep -v 'No common hash found')
  [ -n "$check_filtered" ] && printf '%s\n' "$check_filtered" | grep -E 'ERROR|NOTICE' | tail -20
fi
[ "$check_rc" -eq 0 ] || die "rclone check found differences between $VAULT and $REMOTE/current (rc=$check_rc)"
echo "ops-snapshot: verified $VAULT matches $REMOTE/current at $TS"

# Legacy line kept alongside the per-file one above: readers already grep
# "verified N objects offsite" for the nightly result. Printed only when the
# count is actually known -- a transient `rclone size` failure after a PASSING
# per-file check must never read as "verified 0 objects", which looks like a
# healthy-but-empty remote rather than an unrelated hiccup after a good backup.
remote_n=$(rclone size "$REMOTE/current" --json 2>/dev/null | sed -n 's/.*"count":\([0-9]*\).*/\1/p')
if [ -n "$remote_n" ]; then
  echo "ops-snapshot: verified $remote_n objects offsite at $TS"
else
  echo "ops-snapshot: verified offsite, count unavailable at $TS"
fi

# Freshness marker, written LAST and only once the push AND the per-file verify have
# both succeeded. It lives at $OPS_MARKER, OUTSIDE $VAULT's git tree, so it never gives
# git something to commit on an otherwise-unchanged night.
mkdir -p "$(dirname "$OPS_MARKER")" || die "cannot create $(dirname "$OPS_MARKER")"
printf '{"last_ok":"%s","remote":"%s"}\n' "$(date -Iseconds)" "$REMOTE/current" > "$OPS_MARKER" \
  || die "cannot write $OPS_MARKER"

# ---- claude-memory offsite (separate leg, not part of this vault's git tree) ---
# ~/data-vaults/claude-memory (about 1.4 GB of memory-sync snapshots and
# adjudication records) had no offsite copy of any kind as of 2026-09-29.
# rclone COPY, never sync: this leg must never delete anything offsite, since
# it does not own rotation for that tree the way the notes-vault/lantern/etc
# snapshot scripts own rotation for theirs.
if [ "${OPS_MEMORY_VAULT_OFFSITE:-1}" = "1" ]; then
  MEMORY_SRC="$OPS_HOME/data-vaults/claude-memory"
  # Own directory, never inside $MEMORY_SRC: memory-sync already owns
  # $MEMORY_SRC/last-ok.json (local-tools/memory-sync:605, its own {snapshot,ts}
  # schema, written on every snapshot) and would silently overwrite a marker this
  # script left there, while this script would just as silently overwrite
  # memory-sync's own record right back. If memory-sync has written a
  # last-ok.json inside $MEMORY_SRC, it is simply data this leg backs up like
  # any other file in the tree -- never read or written as THIS leg's marker.
  MEM_MARKER="${OPS_MEM_MARKER:-$OPS_HOME/data-vaults/claude-memory-offsite/last-ok.json}"
  if [ -e "$MEMORY_SRC" ]; then
    mrtype=$(rclone config show "${MEMORY_REMOTE%%:*}" 2>/dev/null | sed -n 's/^type = //p')
    [ "$mrtype" = "crypt" ] || die "refusing claude-memory offsite: remote ${MEMORY_REMOTE%%:*} is type '${mrtype:-unknown}', not crypt" "claude-memory"

    mpre_err=$(rclone lsd --max-depth 1 "$MEMORY_REMOTE" --timeout 20s --contimeout 10s 2>&1 >/dev/null)
    mpre_rc=$?
    if [ "$mpre_rc" -ne 0 ] && [ "$mpre_rc" -ne 3 ]; then
      die "claude-memory offsite preflight failed for $MEMORY_REMOTE (auth or reachability); rclone said: $mpre_err" "claude-memory"
    fi

    mcopy_out=$(rclone copy "$MEMORY_SRC" "$MEMORY_REMOTE/current" \
          --backup-dir "$MEMORY_REMOTE/replaced/$STAMP" \
          "${MEM_FILTER[@]}" --transfers 4 --timeout 30m 2>&1)
    mcopy_rc=$?
    [ -n "$mcopy_out" ] && printf '%s\n' "$mcopy_out" | tail -3
    [ "$mcopy_rc" -eq 0 ] || die "rclone copy of claude-memory to $MEMORY_REMOTE (rc=$mcopy_rc)" "claude-memory"
    echo "ops-snapshot: pushed claude-memory to $MEMORY_REMOTE/current at $TS"

    mcheck_out=$(rclone check --one-way --size-only "${MEM_FILTER[@]}" "$MEMORY_SRC" "$MEMORY_REMOTE/current" 2>&1)
    mcheck_rc=$?
    if [ "$mcheck_rc" -ne 0 ]; then
      mcheck_filtered=$(printf '%s\n' "$mcheck_out" | grep -v 'No common hash found')
      [ -n "$mcheck_filtered" ] && printf '%s\n' "$mcheck_filtered" | grep -E 'ERROR|NOTICE' | tail -20
    fi
    [ "$mcheck_rc" -eq 0 ] || die "rclone check found differences between $MEMORY_SRC and $MEMORY_REMOTE/current (rc=$mcheck_rc)" "claude-memory"
    echo "ops-snapshot: verified $MEMORY_SRC matches $MEMORY_REMOTE/current at $TS"

    # This leg's own freshness marker, written only after ITS push and check both
    # succeed. Separate from $OPS_MARKER on purpose: a claude-memory failure must never
    # touch the ops-vault leg's marker, and a good ops-vault run must never mask a stale
    # or missing claude-memory offsite copy.
    msize_json=$(rclone size ${MEM_FILTER[@]+"${MEM_FILTER[@]}"} "$MEMORY_SRC" --json 2>/dev/null)
    mfiles=$(printf '%s' "$msize_json" | sed -n 's/.*"count":\([0-9]*\).*/\1/p')
    mbytes=$(printf '%s' "$msize_json" | sed -n 's/.*"bytes":\([0-9]*\).*/\1/p')
    mkdir -p "$(dirname "$MEM_MARKER")" || die "cannot create $(dirname "$MEM_MARKER")" "claude-memory"
    printf '{"lane":"claude-memory","last_ok":"%s","files":%s,"bytes":%s,"offsite":true}\n' \
      "$(date -Iseconds)" "${mfiles:-0}" "${mbytes:-0}" > "$MEM_MARKER" \
      || die "cannot write $MEM_MARKER" "claude-memory"
  else
    echo "ops-snapshot: claude-memory offsite skipped, no $MEMORY_SRC"
  fi
else
  echo "ops-snapshot: claude-memory offsite disabled (OPS_MEMORY_VAULT_OFFSITE=0)"
fi

# ---- estate-secrets offsite (independent leg, S1) -------------------------------
# API keys, login files, tunnel credentials, rclone/wrangler configs and per-repo
# .env/.dev.vars, staged fresh every run and pushed encrypted. Never through $VAULT.
# Returns 1 (via fail_leg) instead of exiting so a failure here never blocks the
# audio-notes leg below; the top-level LEGS_FAILED counter is what still fails the
# script at the very end.
run_secrets_leg() {
  local stage n fixed=() envfiles=() p f
  stage=$(mktemp -d "$OPS_HOME/.ops-snapshot-secrets.XXXXXX" 2>/dev/null) \
    || { fail_leg "cannot create secrets staging dir under $OPS_HOME" "estate-secrets"; return 1; }
  chmod 700 "$stage"
  # Guard the trap so it can only ever remove this one mktemp path: the value is
  # captured now (double-quoted, expanded at trap-set time), never reconstructed
  # inside the trap body itself.
  trap "rm -rf '$stage'" EXIT

  for p in ".claude/secrets" ".cloudflared" ".config/rclone/rclone.conf" \
           ".wrangler/config/default.toml" \
           "Library/Preferences/.wrangler/config/default.toml"; do
    [ -e "$OPS_HOME/$p" ] && fixed+=("$p")
  done

  # Every regular file named exactly .env or .dev.vars under $OPS_HOME/Projects,
  # maxdepth 4, excluding _wt/ worktrees, node_modules, .venv, .git, and pruning
  # Projects/mattel-engagement entirely (a client repo, not a real-absence check --
  # the exclusion must hold even on a machine where that repo DOES have a .env).
  while IFS= read -r f; do
    [ -n "$f" ] && envfiles+=("${f#"$OPS_HOME"/}")
  done < <(find "$OPS_HOME/Projects" -maxdepth 4 \
             -path "$OPS_HOME/Projects/mattel-engagement" -prune -o \
             \( -type f \( -name .env -o -name .dev.vars \) -print \) 2>/dev/null \
           | grep -v -e '/_wt/' -e '/node_modules/' -e '/\.venv' -e '/\.git/')

  if [ "${#fixed[@]}" -gt 0 ]; then
    (cd "$OPS_HOME" && rsync -a -R ${fixed[@]+"${fixed[@]}"} "$stage/") \
      || { fail_leg "rsync staging fixed secret paths" "estate-secrets"; return 1; }
  fi
  if [ "${#envfiles[@]}" -gt 0 ]; then
    (cd "$OPS_HOME" && rsync -a -R ${envfiles[@]+"${envfiles[@]}"} "$stage/") \
      || { fail_leg "rsync staging discovered .env/.dev.vars files" "estate-secrets"; return 1; }
  fi
  n=$(find "$stage" -type f 2>/dev/null | wc -l | tr -d ' ')
  echo "ops-snapshot: staged $n secret file(s)"

  local srtype
  srtype=$(rclone config show "${OPS_SECRETS_REMOTE%%:*}" 2>/dev/null | sed -n 's/^type = //p')
  [ "$srtype" = "crypt" ] \
    || { fail_leg "refusing offsite: remote ${OPS_SECRETS_REMOTE%%:*} is type '${srtype:-unknown}', not crypt" "estate-secrets"; return 1; }

  local spre_err spre_rc
  spre_err=$(rclone lsd --max-depth 1 "$OPS_SECRETS_REMOTE" --timeout 20s --contimeout 10s 2>&1 >/dev/null)
  spre_rc=$?
  if [ "$spre_rc" -ne 0 ] && [ "$spre_rc" -ne 3 ]; then
    fail_leg "offsite preflight failed for $OPS_SECRETS_REMOTE (auth or reachability); rclone said: $spre_err" "estate-secrets"
    return 1
  fi

  local ssync_out ssync_rc
  ssync_out=$(rclone sync "$stage" "$OPS_SECRETS_REMOTE/current" \
        --backup-dir "$OPS_SECRETS_REMOTE/replaced/$STAMP" \
        "${SECRETS_FILTER[@]}" --transfers 4 --timeout 5m 2>&1)
  ssync_rc=$?
  [ -n "$ssync_out" ] && printf '%s\n' "$ssync_out" | tail -3
  if [ "$ssync_rc" -ne 0 ]; then
    fail_leg "rclone sync to $OPS_SECRETS_REMOTE (rc=$ssync_rc)" "estate-secrets"
    return 1
  fi
  echo "ops-snapshot: pushed estate-secrets to $OPS_SECRETS_REMOTE/current at $TS"

  local scheck_out scheck_rc scheck_filtered
  scheck_out=$(rclone check --one-way --size-only "${SECRETS_FILTER[@]}" "$stage" "$OPS_SECRETS_REMOTE/current" 2>&1)
  scheck_rc=$?
  if [ "$scheck_rc" -ne 0 ]; then
    scheck_filtered=$(printf '%s\n' "$scheck_out" | grep -v 'No common hash found')
    [ -n "$scheck_filtered" ] && printf '%s\n' "$scheck_filtered" | grep -E 'ERROR|NOTICE' | tail -20
    fail_leg "rclone check found differences between staged secrets and $OPS_SECRETS_REMOTE/current (rc=$scheck_rc)" "estate-secrets"
    return 1
  fi
  echo "ops-snapshot: verified estate-secrets matches $OPS_SECRETS_REMOTE/current at $TS"

  mkdir -p "$(dirname "$OPS_SECRETS_MARKER")" \
    || { fail_leg "cannot create $(dirname "$OPS_SECRETS_MARKER")" "estate-secrets"; return 1; }
  printf '{"lane":"estate-secrets","last_ok":"%s","files":%s,"offsite":true}\n' \
    "$(date -Iseconds)" "$n" > "$OPS_SECRETS_MARKER" \
    || { fail_leg "cannot write $OPS_SECRETS_MARKER" "estate-secrets"; return 1; }
  chmod 600 "$OPS_SECRETS_MARKER" 2>/dev/null
  return 0
}

if [ "$OPS_SECRETS_OFFSITE" = "1" ]; then
  run_secrets_leg || LEGS_FAILED=$((LEGS_FAILED + 1))
else
  echo "ops-snapshot: estate-secrets offsite disabled (OPS_SECRETS_OFFSITE=0)"
fi

# ---- audio-notes offsite (independent leg, S2) -----------------------------------
# ~/.claude/audio-notes (call recordings), excluding the replaceable models/ subdir.
# rclone COPY, never sync/delete -- this leg does not own rotation for this tree,
# same rule as claude-memory. One retry on a failed check: a call may still be
# mid-recording, so a file still growing during the check window is expected, not a
# real failure -- give it exactly one more copy+check cycle before treating it as one.
run_audio_leg() {
  local src="$OPS_HOME/.claude/audio-notes"
  if [ ! -e "$src" ]; then
    echo "ops-snapshot: audio offsite skipped, no $src"
    return 0
  fi

  local artype
  artype=$(rclone config show "${OPS_AUDIO_REMOTE%%:*}" 2>/dev/null | sed -n 's/^type = //p')
  [ "$artype" = "crypt" ] \
    || { fail_leg "refusing audio-notes offsite: remote ${OPS_AUDIO_REMOTE%%:*} is type '${artype:-unknown}', not crypt" "audio-notes"; return 1; }

  local apre_err apre_rc
  apre_err=$(rclone lsd --max-depth 1 "$OPS_AUDIO_REMOTE" --timeout 20s --contimeout 10s 2>&1 >/dev/null)
  apre_rc=$?
  if [ "$apre_rc" -ne 0 ] && [ "$apre_rc" -ne 3 ]; then
    fail_leg "audio-notes offsite preflight failed for $OPS_AUDIO_REMOTE (auth or reachability); rclone said: $apre_err" "audio-notes"
    return 1
  fi

  local acopy_out acopy_rc
  acopy_out=$(rclone copy "$src" "$OPS_AUDIO_REMOTE/current" \
        --backup-dir "$OPS_AUDIO_REMOTE/replaced/$STAMP" \
        "${AUDIO_FILTER[@]}" --transfers 4 --timeout 30m 2>&1)
  acopy_rc=$?
  [ -n "$acopy_out" ] && printf '%s\n' "$acopy_out" | tail -3
  if [ "$acopy_rc" -ne 0 ]; then
    fail_leg "rclone copy of audio-notes to $OPS_AUDIO_REMOTE (rc=$acopy_rc)" "audio-notes"
    return 1
  fi
  echo "ops-snapshot: pushed audio-notes to $OPS_AUDIO_REMOTE/current at $TS"

  local acheck_out acheck_rc
  acheck_out=$(rclone check --one-way --size-only "${AUDIO_FILTER[@]}" "$src" "$OPS_AUDIO_REMOTE/current" 2>&1)
  acheck_rc=$?
  if [ "$acheck_rc" -ne 0 ]; then
    local acopy2_out acopy2_rc
    acopy2_out=$(rclone copy "$src" "$OPS_AUDIO_REMOTE/current" \
          --backup-dir "$OPS_AUDIO_REMOTE/replaced/$STAMP" \
          "${AUDIO_FILTER[@]}" --transfers 4 --timeout 30m 2>&1)
    acopy2_rc=$?
    [ -n "$acopy2_out" ] && printf '%s\n' "$acopy2_out" | tail -3
    if [ "$acopy2_rc" -ne 0 ]; then
      fail_leg "rclone copy of audio-notes to $OPS_AUDIO_REMOTE (rc=$acopy2_rc, retry after failed check)" "audio-notes"
      return 1
    fi
    local acheck2_out acheck2_rc acheck2_filtered
    acheck2_out=$(rclone check --one-way --size-only "${AUDIO_FILTER[@]}" "$src" "$OPS_AUDIO_REMOTE/current" 2>&1)
    acheck2_rc=$?
    if [ "$acheck2_rc" -ne 0 ]; then
      acheck2_filtered=$(printf '%s\n' "$acheck2_out" | grep -v 'No common hash found')
      [ -n "$acheck2_filtered" ] && printf '%s\n' "$acheck2_filtered" | grep -E 'ERROR|NOTICE' | tail -20
      fail_leg "rclone check found differences between $src and $OPS_AUDIO_REMOTE/current after one retry (rc=$acheck2_rc)" "audio-notes"
      return 1
    fi
    echo "ops-snapshot: verified audio-notes matches $OPS_AUDIO_REMOTE/current at $TS (after one retry)"
  else
    echo "ops-snapshot: verified audio-notes matches $OPS_AUDIO_REMOTE/current at $TS"
  fi

  local asize_json afiles abytes
  asize_json=$(rclone size ${AUDIO_FILTER[@]+"${AUDIO_FILTER[@]}"} "$src" --json 2>/dev/null)
  afiles=$(printf '%s' "$asize_json" | sed -n 's/.*"count":\([0-9]*\).*/\1/p')
  abytes=$(printf '%s' "$asize_json" | sed -n 's/.*"bytes":\([0-9]*\).*/\1/p')
  mkdir -p "$(dirname "$OPS_AUDIO_MARKER")" \
    || { fail_leg "cannot create $(dirname "$OPS_AUDIO_MARKER")" "audio-notes"; return 1; }
  printf '{"lane":"audio-notes","last_ok":"%s","files":%s,"bytes":%s,"offsite":true}\n' \
    "$(date -Iseconds)" "${afiles:-0}" "${abytes:-0}" > "$OPS_AUDIO_MARKER" \
    || { fail_leg "cannot write $OPS_AUDIO_MARKER" "audio-notes"; return 1; }
  chmod 600 "$OPS_AUDIO_MARKER" 2>/dev/null
  return 0
}

if [ "$OPS_AUDIO_OFFSITE" = "1" ]; then
  run_audio_leg || LEGS_FAILED=$((LEGS_FAILED + 1))
else
  echo "ops-snapshot: audio-notes offsite disabled (OPS_AUDIO_OFFSITE=0)"
fi

# S3: exit nonzero at the very end if either independent leg failed, even though
# neither one called exit itself and even if the other leg (or the ops-vault/
# claude-memory legs above) printed its own success messaging along the way.
[ "$LEGS_FAILED" -eq 0 ] || exit 1
