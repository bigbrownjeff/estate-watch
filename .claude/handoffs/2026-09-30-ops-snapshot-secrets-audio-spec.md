# ops-snapshot: add secrets and audio-notes offsite legs (SPEC, not implemented)

Status: **spec only**. Nothing in `local-tools/ops-snapshot.sh` has been changed. This
document is the full brief for the session that implements it. Jeff's own instruction for
this task was "Route a, but write it up for another session to implement" plus "API backup
yes, call backup yes" -- so this round only produces the write-up below, on the mandated
worktree/branch, committed and pushed. No code edit, no test edit, no PR.

Worktree used to write this doc: `/Users/jeffpinto/Projects/_wt/ew-secrets-audio` on
`feature/ops-snapshot-secrets-audio` (created fresh from `origin/main` at `c1bdbe7`,
2026-09-30). The implementing session should keep using this worktree/branch, or make a
fresh one from a re-fetched `origin/main` if this one has drifted or was cleaned up.

## Who asked, and for what

Jeff, 2026-09-30, verbatim: "API backup yes. Call backup yes." These are yes answers to two
standing questions -- back up the API keys and login files (encrypted), and back up the 2.3
GB of call recordings (encrypted) -- as two new legs of the existing estate snapshot script.
Today's script deliberately EXCLUDES both (see its own "Excluded on purpose" section in the
generated `RESTORE.md`), so this reverses that exclusion for these two trees only, each as
its own independent, encrypted, never-through-the-plaintext-vault leg.

## What I verified myself before writing this (read-only, no writes, nothing printed below is a secret)

Script (`local-tools/ops-snapshot.sh`, 411 lines) and its test suite
(`local-tools/tests/test_ops_snapshot.sh`, 1187 lines) read in full. Confirmed by running
in this worktree, 2026-09-30:

- `/bin/bash -n local-tools/ops-snapshot.sh` -- clean.
- `/bin/bash local-tools/tests/test_ops_snapshot.sh` -- **SUMMARY: 141 passed, 0 failed**
  (last line). This is the baseline the implementing session's own run should match or
  exceed, minus any deliberate deviation named in S3 below.

Secret and audio sources, checked directly on this machine (names, counts and modes only,
no content printed or copied anywhere):

- `~/.claude/secrets/`: 3 files -- `claude-headless.env` (600), `claude-headless.env.example`
  (644, not sensitive), `lantern-cloud.env` (600).
- `~/.cloudflared/`: 6 tunnel credential `.json` files, each mode 400 (`r--------`); `cert.pem`
  (600); 6 `.yml` configs (agent-lab, atlas, bluecamel, lantern, notes-vault, photos).
- `~/.config/rclone/rclone.conf` (600), `~/.wrangler/config/default.toml` (600),
  `~/Library/Preferences/.wrangler/config/default.toml` (600) -- all present.
- `find ~/Projects -maxdepth 4 \( -name .env -o -name .dev.vars \)`, filtered to exclude any
  path containing `/_wt/`, `/node_modules/`, `/.venv`, `/.git/`: **12 files**, exactly:
  `sous-chef/.dev.vars`, `privacy-judge/.env`, `outbound_with_jeff_and_marv/.env`,
  `outbound_with_jeff_and_marv/cloud-console/.env`, `site-insights/.env`,
  `music-atlas/.env`, `lantern/.env`, `mlb-hits-forecast/.env`,
  `rvc-homeowner-taxes/scripts/.env`, `prevalence-audit-cli/labeling/ls-data/.env`,
  `telegraph/.dev.vars`, `tennis-availability/.dev.vars`. `mattel-engagement` has no such
  file today regardless of the exclusion rule below, so the exclusion cannot be verified by
  absence alone -- implement it as a real path-contains check, not skip-because-empty.
- `~/.claude/audio-notes`: 2.3G total, `models/` alone 465M (confirmed excludable, the whisper
  models are replaceable). Files outside `models/`: 6101. Symlinks anywhere in the tree: 0
  (so `-L`/no-follow-symlink choices don't matter here the way they did for `.local/bin`).

Existing test fixture already builds `.claude/secrets/` and `.cloudflared/` into its fake
HOME (`test_ops_snapshot.sh` lines 79-101) with fake content (`API_KEY=notreal`,
`not-a-real-cert`) and asserts at lines 280-284 that neither leaks into the ops-vault tree,
plus a mutation-proof at lines 609-623 that the allowlist check itself would flag `secrets`
if someone added it to `sync_dir`. **These assertions are about the ops-vault leg
specifically and must stay green, unchanged, after this work** -- they are the proof that
the new secrets leg (S1) never touches `$VAULT`. Do not "fix" them to expect secrets inside
the vault; that would be the regression, not the fix.

## Hard rules for the implementing session (carried forward verbatim, they still apply)

- Cap the lane at about 20 tool turns of verify/fix; at the cap, stop and report state
  (done, remaining, exact next command) rather than looping.
- No subagents, no forking. Synchronous, foreground only, no background tasks.
- Work only inside a real worktree (`git worktree list` + `pwd` before any edit), never on
  the `estate-watch` canonical checkout.
- Never touch production: no `gdw:`/`gdw-crypt:` path, nothing under `~/data-vaults`,
  `~/ops-vault`, `~/.claude`, `~/My Drive`, no real secret file, no `crm.db` write. Every
  script RUN (not read) uses a fixture `HOME`/`OPS_HOME` under `mktemp` and an isolated
  `RCLONE_CONFIG` with a crypt remote over a local temp dir, same pattern the existing suite
  already uses (`test_ops_snapshot.sh` lines 55-74 -- reuse that shape, do not invent a new
  one). Never run any installed tool under `~/.claude/bin`, not even `--help`. Never run
  `wrangler`. Never install or load a launchd job. Never open a PR, never merge.
- Never print, copy or commit a real secret value. Fixture secrets are obviously-fake
  invented strings (the existing suite's `API_KEY=notreal` is the model).
- Runtime target is macOS `/bin/bash` 3.2 under launchd's environment: guard every array
  expansion as `${arr[@]+"${arr[@]}"}` (see `sync_dir`'s `${ex[@]+"${ex[@]}"}` and the
  claude-memory leg's `${MEM_FILTER[@]+"${MEM_FILTER[@]}"}` for the two idioms already in
  the file -- match whichever the surrounding code already uses), no associative arrays, no
  `mapfile`, BSD `date`/`stat` only, no `timeout(1)`.
- House rule: no em dashes or en dashes anywhere written, including in the script's own
  comments and the restore doc.
- Mutate a COPY to prove a test, restore from a `cp` copy; never `git checkout`, never `git
  stash`.
- Commit on the branch with a conventional message and push
  `-u origin feature/ops-snapshot-secrets-audio`, trailer:
  ```
  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01QCHEB2v9bhXgsFXJitD2cw
  ```
  (confirm the session URL against whatever session is actually doing the implementing --
  do not blindly copy this one if a different session picks up the work).
- **Split code from tests.** This spec's own deliverable boundary (S1-S5 below) is code only
  in `local-tools/ops-snapshot.sh`. Writing or updating `local-tools/tests/test_ops_snapshot.sh`
  is a separate lane/session, briefed separately once the code lane reports done. Don't let
  one session try to do both; that is exactly the pattern that has blown 40-turn caps on this
  estate before.

## The two new legs

### S1 -- secrets leg

New env vars (top of script, alongside the existing `OPS_HOME`/`VAULT`/`REMOTE` block):

```bash
OPS_SECRETS_OFFSITE="${OPS_SECRETS_OFFSITE:-1}"
OPS_SECRETS_REMOTE="${OPS_SECRETS_REMOTE:-gdw-crypt:estate-secrets}"
OPS_SECRETS_MARKER="${OPS_SECRETS_MARKER:-$OPS_HOME/data-vaults/estate-secrets-offsite/last-ok.json}"
SECRETS_FILTER=(--exclude '.DS_Store')
```

Sources, discovered fresh on every run, all relative to `$OPS_HOME`:

- `.claude/secrets/`
- `.cloudflared/`
- `.config/rclone/rclone.conf`
- `.wrangler/config/default.toml`
- `Library/Preferences/.wrangler/config/default.toml`
- every regular file named exactly `.env` or `.dev.vars` under `$OPS_HOME/Projects` at
  `-maxdepth 4`, excluding any path containing `/_wt/`, `/node_modules/`, `/.venv`, `/.git/`,
  and excluding the repo directory `Projects/mattel-engagement` entirely.

A missing source is skipped quietly (same contract as `sync_dir`'s
`[ -e "$1" ] || return 0`). Never follow a symlink (no `-L`, no `-l`; match the `.local/bin`
leg's `--no-links` idiom at lines 166-176, or simply don't add link-following flags to
whichever copy command is used -- `find`/`cp -p` naturally does not follow symlinks for a
plain file match).

Staging: `mktemp -d` created **under `$OPS_HOME`** (not `/tmp`) with mode 700 so it never
lands outside the user's own tree, files copied in preserving their relative path from
`$OPS_HOME` and their mode. `rsync -a -R` is the natural tool for "preserve relative path" --
confirm `-R` (relative) behaves as expected for each of the five fixed paths plus the
`find`-discovered `.env`/`.dev.vars` list (likely two `rsync` invocations: one for the fixed
paths, one for the discovered list, both writing into the same stage dir with `-R` relative
to `$OPS_HOME` via `cd "$OPS_HOME" && rsync -a -R <relative-paths...> "$STAGE/"`). Remove the
stage on `EXIT` via `trap`, and guard the trap so it can only ever remove that one `mktemp`
path (capture the exact path in a variable set once, never reconstruct it in the trap).

The staged secrets must never enter `$VAULT`/`~/ops-vault` or its git history, and must never
be printed anywhere (log lines carry counts only, e.g. "staged 24 secret files").

Push, mirroring the existing ops-vault push+check shape exactly (lines 283-331 of the current
script are the template):

1. Crypt-type refusal: `rclone config show "${OPS_SECRETS_REMOTE%%:*}"` must report
   `type = crypt`, else `die "refusing offsite: remote ... is type '...', not crypt"
   "estate-secrets"`.
2. Preflight: `rclone lsd --max-depth 1 "$OPS_SECRETS_REMOTE" --timeout 20s --contimeout
   10s`; rc 0 or 3 (empty-but-reachable) both pass, anything else dies with the
   `"estate-secrets"` lane.
3. `rclone sync "$STAGE" "$OPS_SECRETS_REMOTE/current" --backup-dir
   "$OPS_SECRETS_REMOTE/replaced/$STAMP" "${SECRETS_FILTER[@]}" --transfers 4 --timeout 5m`,
   rc captured directly (never through a pipe -- this exact bug class was already found and
   fixed once in the ops-vault leg per the comment at lines 301-305, do not reintroduce it
   here).
4. `rclone check --one-way --size-only "${SECRETS_FILTER[@]}" "$STAGE"
   "$OPS_SECRETS_REMOTE/current"`, same filter as the push. Filter out the expected `"No
   common hash found"` NOTICE before deciding whether there is anything to print (S5).
5. Marker, written only after the check passes, mode 600:
   `{"lane":"estate-secrets","last_ok":"<ISO ts>","files":<n>,"offsite":true}` at
   `$OPS_SECRETS_MARKER` (create its parent dir first).

### S2 -- audio leg

New env vars:

```bash
OPS_AUDIO_OFFSITE="${OPS_AUDIO_OFFSITE:-1}"
OPS_AUDIO_REMOTE="${OPS_AUDIO_REMOTE:-gdw-crypt:data-vaults/audio-notes}"
OPS_AUDIO_MARKER="${OPS_AUDIO_MARKER:-$OPS_HOME/data-vaults/audio-notes-offsite/last-ok.json}"
AUDIO_FILTER=(--exclude 'models/**' --exclude '.DS_Store')
```

Source: `$OPS_HOME/.claude/audio-notes`. Skip quietly (no error, no die) when the directory
does not exist, same contract as the claude-memory leg's own `if [ -e "$MEMORY_SRC" ]`
branch (lines 368-408) -- follow that exact shape, including its else-branch log line
("audio offsite skipped, no ...").

Push is `rclone copy`, never `sync` -- this leg must never delete anything offsite, same
rule and same reasoning as the claude-memory leg (comment at lines 355-357: it does not own
rotation for this tree). `--backup-dir "$OPS_AUDIO_REMOTE/replaced/$STAMP"`, captured rc,
`--timeout 30m` is what claude-memory uses for its ~1.4 GB; audio is 2.3 GB total but ~1.8 GB
after excluding `models/`, so keep 30m or raise slightly -- pick one and say why in the
commit message, don't leave it unconsidered. Add `--timeout` for the whole run as
claude-memory already does; the "idle --timeout 30m" language in the brief is the same
per-transfer idle timeout rclone already applies via `--timeout`, not a new flag -- confirm
against `rclone copy --help` semantics for `--timeout` (idle timeout per transfer) before
assuming a different flag is needed.

Check-with-retry-once, the one piece of logic genuinely new to this script (no existing leg
does this):

```
rclone check --one-way --size-only "${AUDIO_FILTER[@]}" "$SRC" "$REMOTE/current"
if check failed:
    # a call may still be mid-recording; one recording growing during the window
    # is expected, not a failure -- give it exactly one more copy+check cycle
    rerun the copy (same command as above)
    rerun the check (same command as above)
    if this second check ALSO fails:
        print rclone's ERROR and NOTICE lines (names only, filtered same as elsewhere)
        die "..." "audio-notes"
    # else: second check passed, proceed as normal
```

Marker, written only after a passing check (first or second attempt), mode 600:
`{"lane":"audio-notes","last_ok":"<ISO ts>","files":<n>,"bytes":<n>,"offsite":true}` at
`$OPS_AUDIO_MARKER`, counted with `${AUDIO_FILTER[@]+"${AUDIO_FILTER[@]}"}` via `rclone
size --json` the same way the claude-memory leg already does (lines 399-401) -- reuse that
exact `sed` extraction pattern for `count`/`bytes`.

### Shared implementation note: no existing helper to "reuse", only a shape to copy

The brief for S1 says "reuse the existing helper or shape" for the crypt-refusal-plus-preflight
check. There is **no separate function today** -- the ops-vault leg (lines 279-299) and the
claude-memory leg (lines 369-376) each inline the same two checks. With this change there will
be four legs doing it. Recommend factoring a small helper, e.g.:

```bash
crypt_preflight() {  # $1 remote  $2 lane (for die())
  local remote="$1" lane="$2" rtype pre_err pre_rc
  rtype=$(rclone config show "${remote%%:*}" 2>/dev/null | sed -n 's/^type = //p')
  [ "$rtype" = "crypt" ] || die "refusing offsite: remote ${remote%%:*} is type '${rtype:-unknown}', not crypt" "$lane"
  pre_err=$(rclone lsd --max-depth 1 "$remote" --timeout 20s --contimeout 10s 2>&1 >/dev/null)
  pre_rc=$?
  [ "$pre_rc" -eq 0 ] || [ "$pre_rc" -eq 3 ] || die "offsite preflight failed for $remote (auth or reachability); rclone said: $pre_err" "$lane"
}
```

This is an improvement, not a requirement -- if the implementing session judges the inline
duplication safer to review as a diff (matches the file's existing style, smaller blast
radius on the two untouched legs), leaving all four inline is an acceptable, explicitly-noted
deviation. Either way, do not refactor the two EXISTING legs' inline blocks into the helper
in this same change unless it is trivial and reviewed -- that would widen the diff into code
that already has passing tests pinned to its exact current shape (see `die()` note below).

### `die()` needs a third and fourth lane, not just the current if/else

Today (lines 54-71) `die()` branches only on `lane == "claude-memory"` vs everything else
(which gets the generic `"ops-snapshot failed"` / `ops-snapshot-failed` title). It needs to
become a proper dispatch with **four** lanes: the existing default (`"ops"`, unchanged text),
the existing `"claude-memory"` (unchanged text -- two mutation-proof tests, T14a/T14b at the
end of the suite, pin this exact branch and must stay green), plus new `"estate-secrets"` and
`"audio-notes"` branches with their own title/dedupe/detail text per S3 below. A `case`
statement is the natural replacement for the `if`; preserve the existing two branches'
strings byte for byte so the two mutation-proof tests keep passing unmodified.

## S3 -- independent legs, own failure identity

Each of the four legs (ops-vault unchanged, claude-memory unchanged, secrets new, audio new)
records its own failure with its own title and its own dedupe key:

- `claude-memory-offsite-failed` (existing, unchanged)
- `estate-secrets-offsite-failed` (new)
- `audio-notes-offsite-failed` (new)
- the default/ops-vault key (existing, unchanged -- `ops-snapshot-failed`)

No leg ever removes or rewrites another leg's marker file. No leg's failure stops the legs
that run after it (`die()` currently calls `exit 1` immediately -- **this is a real
structural change**: today ANY `die()` call halts the whole script, which is correct for the
ops-vault leg blocking claude-memory today by accident of ordering, but S3 requires the
opposite for the new legs. The implementing session needs to either (a) stop calling `die()`
directly inside the secrets/audio blocks and instead capture "this leg failed" into a local
flag + a leg-scoped file-a-task-and-continue helper, or (b) restructure each leg's fallible
steps as their own subshell/function that returns nonzero instead of exiting, with the
top-level script tracking a `LEGS_FAILED` counter and calling the real `die()`-style
file-a-task-and-exit only once, at the very end, if any leg failed. Option (b) is cleaner and
keeps `die()`'s job (file the right task under the right key) separate from "stop everything",
which today it conflates. Recommend renaming the failure-filer to something like
`fail_leg "$msg" "$lane"` (files the task, prints, returns 1 -- does NOT exit) and keeping a
thin `die()` wrapper (`fail_leg "$@"; exit 1`) for the two remaining legs that should still
hard-stop (ops-vault init failures like `mkdir -p "$VAULT"` -- if the vault itself can't be
created there is no point attempting any leg). This needs a design decision, not just a
mechanical edit; flag it explicitly in the implementing session's own report rather than
silently picking one.

Script exits nonzero at the very end if any leg failed, even if every individual leg printed
its own recovery/skip messaging along the way.

The ops-vault leg keeps its current behavior (hard stop on its own failure, as today).

## S4 -- header comment and generated restore doc

Header comment (top of script, currently lines 1-18): add that secrets and audio are no
longer excluded, on Jeff's 2026-09-30 ruling, and name the one exclusion
(`Projects/mattel-engagement`, a client repo whose local overlay stays off any offsite copy).
Keep the existing "Design (option E)" and "Deletions are never destructive" paragraphs; this
is an addition, not a rewrite.

`RESTORE.md` (generated inside the script at lines 181-264, never hand-edited): add two new
rows to the existing three-row table (`| Copy | Location | Survives |`) for the secrets and
audio offsite copies, and two new numbered restore procedures alongside the existing
"Rebuild on a new Mac" (steps 1-7) and "claude-memory offsite" sections -- **each restored
secret file must get its mode set back explicitly with `chmod 600`** after `rclone copy` off
the offsite remote (rclone does not reliably round-trip original Unix modes through a crypt
remote the way local `rsync -a` does within `~/ops-vault`; do not assume it does without
checking `rclone copy --help` / `--metadata` support for the version pinned on this machine).
Update the "Excluded on purpose" section to drop the "audio-notes" reference (it's the
`models/` subdirectory now, not the whole tree) and to state that secrets are covered by
their own leg, same pattern already used there for `claude-memory`.

## S5 -- quiet-success discipline

Both new legs must follow the existing idiom exactly: `[ -n "$out" ] && printf '%s\n' "$out"
| tail -3` after a push (nothing printed when rclone is silent), and filter `"No common hash
found"` out of check output before deciding whether there's anything to print on failure
(`grep -v 'No common hash found'`, matching lines 327 and 389 of the current script). No new
blank-line-on-success paths.

## Report shape expected from the implementing (code) lane

One entry per S1-S5 (DONE/PARTIAL/NOT_DONE + how), `last_result_line` = the final line of
`/bin/bash local-tools/tests/test_ops_snapshot.sh` run against the untouched, current suite
(it will almost certainly need edits to keep passing once `die()`'s shape changes under S3 --
if the existing suite goes red because of an intentional S3 change, that is a `deviations`
entry, not a silent fix, since editing the test file is explicitly the NEXT lane's job, not
this one's). `/bin/bash -n` on the edited script is the other required check. Do not write or
edit `local-tools/tests/test_ops_snapshot.sh` in the code lane; a stale-suite report is
expected and correct there.

## What this session did NOT do (by design, per Jeff's "write it up" instruction)

- Did not edit `local-tools/ops-snapshot.sh`.
- Did not write or run any new tests.
- Did not open a PR.
- Did commit and push this spec document to `feature/ops-snapshot-secrets-audio`.
