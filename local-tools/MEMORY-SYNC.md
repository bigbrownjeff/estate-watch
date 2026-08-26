# memory-sync — one doctrine across three Claude profiles

Jeff runs three Claude profiles, each with its own memory store:

| profile     | store |
|-------------|-------|
| `main`      | `~/.claude/projects/-Users-jeffpinto/memory/` |
| `claudine`  | `~/.claude-claudine/projects/-Users-jeffpinto/memory/` |
| `claudette` | `~/.claude-claudette/projects/-Users-jeffpinto/memory/` |

Each store is one-fact-per-file markdown plus a `MEMORY.md` index that is loaded
into every session of that profile. A rule Jeff issues in one profile is
invisible to the other two until someone copies it by hand.

**The incident that built this (2026-08-26).** `persona-check-is-rule-zero.md`
was hand-copied into all three stores, and the three copies were *not* the same
file: one cited "the Fable spawn rule", another "RULE #1"; only one carried the
`[[...]]` cross-links; only one recorded which profile issued it. Three profiles,
three subtly different versions of one binding rule.

## What syncs, and why

The rule was derived by reading all 134 memories, not assumed.

| type | syncs | why |
|------|-------|-----|
| `feedback` | **yes** | Rules about *how to work with Jeff*. Profile-agnostic by construction. A missing one makes a profile repeat a correction Jeff already paid for once. |
| `user` | **yes** | Facts about Jeff the person ("hates R.E.M.", "never put his street address in a draft"). True in every profile. |
| `project` | no | Current workstream state carrying dates that expire — "sendable ~Aug 26-28", "go/no-go 08-24". Churns constantly, and that churn is the concurrent-write hazard. |
| `reference` | no | Pointers into external systems whose access is itself profile-scoped (which MCPs are wired differs per profile), and several are secret-adjacent. Replicating them widens that surface for little gain. |

### Marking exceptions

Per file, in the frontmatter `metadata:` block:

```yaml
metadata:
  type: project
  sync: true      # force this one to travel to every profile
```
```yaml
metadata:
  type: feedback
  sync: false     # pin this one to its own profile; never send, never receive
```

`sync: false` anywhere pins the memory in **every** store — an opt-out cannot be
overridden by another profile's copy.

## The merge ladder — content only, never a version header

Both files that had drifted between `main` and `claudine` carried
`metadata.modified` stamps that could not be trusted:
`client-email-voice-deltas.md` had **identical** stamps on both sides with 20KB
of body missing from one. So `modified` is printed as evidence for a human and is
never used to pick a winner.

1. identical sha1 → no-op
2. absent on a target → **ADD** (provably lossless)
3. one copy is a strict line-superset of every other → **PROMOTE** (provably
   lossless: every line of the loser survives in the winner)
4. anything else → **CONFLICT**. Nothing is written, ever.

Only 2 and 3 run unattended. Rule 4 is the unattended-judgment ban: an automated
process may propagate an addition; it may never adjudicate ambiguity. On a
conflict, byte-exact copies of every side are written to
`~/data-vaults/claude-memory/conflicts/` and a board task is filed.

**Deletions never propagate.** A file present in ≥1 store is pushed to all. To
retire a doctrine memory everywhere: `memory-sync --retire <name> --apply`.

## MEMORY.md index handling

For a memory being added or promoted, the source store's index line is copied to
any store lacking one (appended), or replaces that store's line in place if it
differs. Lines belonging to memories that are not syncing are never touched, and
a link is never duplicated. Composing a *new* index hook, or choosing between two
duplicate lines, is judgment — the tool reports those as `NOTE` and leaves them.

## Concurrency

Live sessions write these directories while the job runs. Guards:

- a lockdir at `~/.claude/failures/.memory-sync.lockdir` (stale after 30 min)
- every write is temp-file + `os.replace`, so a reader never sees a half file
- read-modify-write on `MEMORY.md` re-verifies the file did not change since it
  was read, and backs off rather than clobbering
- a file touched in the last 120 seconds is **deferred** to the next run

## Schedule

`com.jeffpinto.memory-sync`, daily **08:45** — before the workday's first session
loads a store, and after the 02:30 ops-snapshot. Wrapped in `runlog`; conflicts
file a `failtask` under project `infra`. Logs: `~/.claude/logs/memory-sync.log`.

## Recovery

Every apply tars all three stores first, to
`~/data-vaults/claude-memory/snapshots/` (last 30 kept), and records the newest
in `last-ok.json`. To roll back the whole estate to a snapshot:

```bash
tar xzf ~/data-vaults/claude-memory/snapshots/<snapshot>.tar.gz -C /tmp/mem-restore
rsync -a --delete /tmp/mem-restore/main/      ~/.claude/projects/-Users-jeffpinto/memory/
rsync -a --delete /tmp/mem-restore/claudine/  ~/.claude-claudine/projects/-Users-jeffpinto/memory/
rsync -a --delete /tmp/mem-restore/claudette/ ~/.claude-claudette/projects/-Users-jeffpinto/memory/
```

To restore one file, pull it out of the tar and copy it into place — the archive
paths are `<profile>/<name>.md`.

## Commands

```
memory-sync                     dry run: print the plan, write nothing
memory-sync --verbose           ... with per-file diffs and evidence
memory-sync --apply             snapshot, then apply ADD/PROMOTE
memory-sync --check             exit 1 on drift, 2 on conflict (for monitors)
memory-sync --scheduled         launchd mode: apply lossless, failtask the rest
memory-sync --retire NAME       remove a memory + index line from all stores
memory-sync --selfcheck         is the installed copy identical to the repo source
```

Install: `install -m 0755 ~/Projects/estate-watch/local-tools/memory-sync ~/.claude/bin/memory-sync`
