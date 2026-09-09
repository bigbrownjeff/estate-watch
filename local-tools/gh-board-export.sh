#!/bin/bash
# gh-board-export — nightly offsite backup of GitHub Project #1 "Jeff - All Projects"
# (github.com/users/bigbrownjeff/projects/1, user-scoped).
#
# WHY THIS EXISTS. 2026-08-27, Jeff: "If we're not backing that up offsite let's solve
# that via a nightly." The board is the estate's single todo/failure/decision surface
# (see ~/.claude/failures/README.md, the failtask contract) and lived only on GitHub's
# servers with no local or offsite copy.
#
# Design, mirrors ~/Projects/notes-vault/ops/data_snapshot.sh and
# ~/Projects/family-archive/ops/data_snapshot.sh:
#   - Dated snapshot dirs under $VAULT/YYYY-MM-DD/ (one per day, not hardlink-deduped —
#     the board is small: ~470 items, a few hundred KB of JSON, dedup isn't worth the
#     complexity here).
#   - items.json + fields.json via `gh project item-list` / `field-list` (REST-shaped
#     JSON the gh CLI already normalizes).
#   - views.json via a direct GraphQL query (item-list/field-list don't expose per-view
#     filter/sort/group). If a queried sub-field 404s (schema drift), the script retries
#     with that field dropped and records the omission in views.json rather than failing
#     the whole backup — a partial board backup beats a lost cycle over one broken field.
#   - manifest.json: counts + sha256 of each file + timestamp, so a stale/short backup
#     is a completed contract we can verify rather than trusted from exit code alone.
#   - Offsite via the SAME rclone gdw-crypt: remote every other estate snapshot lane
#     uses (ops-vault, notes-vault, family-archive) — client-side encrypted before
#     anything leaves this machine. This board holds client-sensitive todo detail.
#   - 30-day local retention (prune older dated dirs after a successful run).
#
# Deletions offsite are never destructive: rclone sync writes replaced/removed files to
# a dated --backup-dir instead of discarding them.
#
# RESTORE MODEL (Issue-backed board, T-1742 conversion, board-issues-plan.md section D
# lane L4). Project #1's cards are backed by real Issues in BOARD_REPO below (default
# bigbrownjeff/board), not DraftIssues, once T-1742 converts them. This changes what a
# restore from this backup can and cannot do:
#   - `gh project item-create` mints a DraftIssue and is the WRONG restore path on an
#     Issue-backed board. Recreating a card means `gh issue create --repo "$BOARD_REPO"`
#     then `gh project item-add`, per the generated README's restore section below.
#   - Every recreated item gets a BRAND-NEW PVTI_ item id. The old id is gone forever;
#     recreation is not resurrection. Anything keyed on the old id (crm.db
#     tasks.board_item_id, ~/.claude/failures/board-cache.json, any handoff note citing
#     a PVTI_) goes stale and needs an explicit old-id -> new-id backfill, rehearsed on a
#     /tmp copy of crm.db first (skill dry-run-backfill, memory backup-before-anything-else)
#     before it ever touches the real database. See the README's rebuild section.
#   - Converting a draft to an issue has no inverse mutation, and neither does this
#     restore path: there is no un-convert. A restore is a REBUILD from data.json, not an
#     un-delete, and a full-board rebuild is measured in hours, not minutes (see
#     board-issues-plan.md section C.7). This backup format optimizes for "the data
#     survives," not "one-command undo."
#   - comments.json (board-issues-plan.md section D lane L4, second half, section E.3)
#     captures every issue's comment thread: one REST call per issue (never one big
#     GraphQL join, so a single slow/bad issue costs one comment thread, not the whole
#     pass), paced ($GH_BOARD_COMMENTS_PACE, default 0.4s) to stay clear of GitHub's
#     secondary rate limit, resumable within a run via per-issue part files under the
#     dated $DEST (never a cross-day cache — that would hide new comments), and bounded
#     by a wall-clock budget ($GH_BOARD_COMMENTS_BUDGET_SECS, default 1800s) so an
#     unusually large board can't block the nightly job indefinitely: a run that hits
#     the budget records "partial" in comments.json and resumes from its part files the
#     next time the script runs the same day. Skips cleanly (never silently) to a
#     one-line placeholder when GH_BOARD_REPO="" is set.
set -u
# Hardened by default (a launchd script does not trust an inherited PATH).
# GH_BOARD_TEST_PATH exists only so the offline test harness can prepend a
# directory of stubbed `gh`/`board-ref` binaries; never set in production.
export PATH="${GH_BOARD_TEST_PATH:-/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}"

OWNER="${GH_BOARD_OWNER:-bigbrownjeff}"
NUMBER="${GH_BOARD_NUMBER:-1}"
VAULT="${GH_BOARD_VAULT:-$HOME/data-vaults/gh-board}"
KEEP_DAYS="${GH_BOARD_KEEP_DAYS:-30}"
OFFSITE="${GH_BOARD_OFFSITE:-1}"
REMOTE="${GH_BOARD_REMOTE:-gdw-crypt:data-vaults/gh-board}"
# Single source of truth for the restore doc below (review fix 1 shape: one config
# constant, never a literal at each call site). Not used by capture, only by the
# generated restore instructions, until this script itself gains a create path.
# Single-dash default (not :-): GH_BOARD_REPO="" must stay empty so a caller can
# deliberately disable comments capture (tested below); :- would treat an
# explicitly empty string the same as unset and silently re-default it.
BOARD_REPO="${GH_BOARD_REPO-bigbrownjeff/board}"

TS=$(date '+%Y-%m-%d %H:%M:%S')
DATE=$(date '+%Y-%m-%d')
FAILTASK="${GH_BOARD_FAILTASK:-$HOME/.claude/bin/failtask}"
LOG="$HOME/.claude/failures/gh-board-export.log"
mkdir -p "$(dirname "$LOG")"

die() {
  echo "gh-board-export: FAILED — $1" | tee -a "$LOG"
  tail_ctx=$(tail -20 "$LOG" 2>/dev/null)
  if [ -x "$FAILTASK" ]; then
    "$FAILTASK" gh-board "gh board export failed" \
      --detail "The nightly GitHub Project #1 (Jeff - All Projects) backup did not complete at $TS. Reason: $1. Log tail:
$tail_ctx" \
      --dedupe-key gh-board-export-failed --severity error >/dev/null 2>&1
  fi
  exit 1
}

command -v gh >/dev/null 2>&1 || die "gh CLI not found on PATH"
case "$VAULT" in ""|"/"|"/*") die "refusing to use VAULT='$VAULT'";; esac

DEST="$VAULT/$DATE"
mkdir -p "$DEST" || die "cannot create $DEST"

# ---- 0. board-ref backfill (before capture, so the export carries Refs) -----------
BOARD_REF="${GH_BOARD_REF_BIN:-$HOME/.claude/bin/board-ref}"
if [ -x "$BOARD_REF" ]; then
  "$BOARD_REF" --backfill >>"$LOG" 2>&1 || die "board-ref --backfill exited nonzero"
else
  die "board-ref not found or not executable at $BOARD_REF"
fi

# ---- 1. items.json --------------------------------------------------------------
# --limit is a hard cap, not a page size: at 1000 the board (1,143 items on
# 2026-09-09) was silently truncated and every sweep read a short list.
gh project item-list "$NUMBER" --owner "$OWNER" --limit 5000 --format json \
  > "$DEST/items.json.tmp" 2>>"$LOG" || die "gh project item-list failed"
[ -s "$DEST/items.json.tmp" ] || die "items.json is empty"
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$DEST/items.json.tmp" \
  || die "items.json is not valid JSON"
mv "$DEST/items.json.tmp" "$DEST/items.json"
ITEM_COUNT=$(python3 -c "import json; print(len(json.load(open('$DEST/items.json'))['items']))") \
  || die "cannot count items.json"
[ -n "$ITEM_COUNT" ] && [ "$ITEM_COUNT" -gt 0 ] || die "items.json has 0 items"
TOTAL_COUNT=$(python3 -c "import json; print(json.load(open('$DEST/items.json')).get('totalCount', 0))") \
  || die "cannot read totalCount"
[ "$ITEM_COUNT" -eq "$TOTAL_COUNT" ] \
  || die "items.json is TRUNCATED: $ITEM_COUNT of $TOTAL_COUNT items (raise --limit)"

# ---- 2. fields.json ---------------------------------------------------------------
gh project field-list "$NUMBER" --owner "$OWNER" --format json \
  > "$DEST/fields.json.tmp" 2>>"$LOG" || die "gh project field-list failed"
[ -s "$DEST/fields.json.tmp" ] || die "fields.json is empty"
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$DEST/fields.json.tmp" \
  || die "fields.json is not valid JSON"
mv "$DEST/fields.json.tmp" "$DEST/fields.json"
FIELD_COUNT=$(python3 -c "import json; print(len(json.load(open('$DEST/fields.json'))['fields']))") \
  || die "cannot count fields.json"
[ -n "$FIELD_COUNT" ] && [ "$FIELD_COUNT" -gt 0 ] || die "fields.json has 0 fields"

# ---- 3. views.json (GraphQL) -------------------------------------------------------
# Full query first; on failure, drop sortBy/groupBy sub-fields one at a time and
# record what was omitted, rather than failing the whole nightly over a schema
# drift in one nested field. filter/layout/name are the load-bearing part.
VIEWS_NOTE=""
FULL_QUERY='
query($login:String!, $number:Int!){
  user(login:$login){
    projectV2(number:$number){
      views(first:50){
        nodes{
          name
          layout
          filter
          sortByFields(first:20){ nodes{ field{ ... on ProjectV2FieldCommon { name } } direction } }
          groupByFields(first:20){ nodes{ ... on ProjectV2FieldCommon { name } } }
        }
      }
    }
  }
}'
if gh api graphql -f query="$FULL_QUERY" -f login="$OWNER" -F number="$NUMBER" \
     > "$DEST/views.json.tmp" 2>>"$LOG"; then
  :
else
  echo "gh-board-export: full views query failed, retrying without sortBy/groupBy" | tee -a "$LOG"
  REDUCED_QUERY='
query($login:String!, $number:Int!){
  user(login:$login){
    projectV2(number:$number){
      views(first:50){
        nodes{ name layout filter }
      }
    }
  }
}'
  if gh api graphql -f query="$REDUCED_QUERY" -f login="$OWNER" -F number="$NUMBER" \
       > "$DEST/views.json.tmp" 2>>"$LOG"; then
    VIEWS_NOTE="sortByFields/groupByFields were not queryable on this run; recorded name/layout/filter only"
  else
    die "views GraphQL query failed even in reduced form"
  fi
fi
[ -s "$DEST/views.json.tmp" ] || die "views.json is empty"
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$DEST/views.json.tmp" \
  || die "views.json is not valid JSON"
VIEW_COUNT=$(python3 -c "
import json
d = json.load(open('$DEST/views.json.tmp'))
print(len(d.get('data', {}).get('user', {}).get('projectV2', {}).get('views', {}).get('nodes', [])))
") || die "cannot count views.json"
if [ -n "$VIEWS_NOTE" ]; then
  python3 -c "
import json
d = json.load(open('$DEST/views.json.tmp'))
d['_export_note'] = '''$VIEWS_NOTE'''
json.dump(d, open('$DEST/views.json.tmp', 'w'), indent=2)
" || die "cannot annotate views.json"
fi
mv "$DEST/views.json.tmp" "$DEST/views.json"
[ -n "$VIEW_COUNT" ] && [ "$VIEW_COUNT" -gt 0 ] || die "views.json has 0 views"

# ---- 4. comments.json (Issue-backed board, T-1742 lane L4 second half) -----------
# One REST call per issue (not one big GraphQL join): each issue is fetched
# independently so a single bad/slow issue doesn't cost the whole pass, it's
# resumable within a run via per-issue part files under $DEST (a fresh, empty
# dir every day — resumability is for a crash/restart THIS run, never a
# permanent cache that would hide new comments on a later night), and it's
# paced to stay well clear of GitHub's secondary rate limit (memory
# github-graphql-secondary-limit, the 2026-08-27 trip). Skips cleanly, never
# silently, when no board repo is configured (GH_BOARD_REPO="").
COMMENTS_PACE="${GH_BOARD_COMMENTS_PACE:-0.4}"
COMMENTS_BUDGET_SECS="${GH_BOARD_COMMENTS_BUDGET_SECS:-1800}"
PARTS_DIR="$DEST/.comments-parts"
# Manifest carries the comments summary either way (a skip string, or the capture
# stats below) — no comments.json FILE is written when the repo is unset, so the
# unset case stays a clean three-artifact export, never a fourth file full of a
# placeholder. This is the "explicit skip line," not a silent absence.
COMMENTS_MANIFEST_NOTE=""

if [ -z "$BOARD_REPO" ]; then
  COMMENTS_MANIFEST_NOTE='"skipped (no board repo configured)"'
  echo "gh-board-export: comments capture skipped, no board repo configured (GH_BOARD_REPO is unset)" | tee -a "$LOG"
else
  mkdir -p "$PARTS_DIR" || die "cannot create $PARTS_DIR"
  ISSUE_NUMBERS_FILE=$(mktemp) || die "mktemp for issue numbers"
  # -X GET is required: gh api silently switches to POST once any -f is
  # present unless the method is pinned, which would hit the CREATE-issue
  # endpoint instead of listing (caught live during this lane's build: it
  # returned a 422 "title wasn't supplied").
  gh api -X GET "repos/$BOARD_REPO/issues" --paginate -f state=all -f per_page=100 \
      --jq '.[] | select(has("pull_request")|not) | .number' \
      > "$ISSUE_NUMBERS_FILE" 2>>"$LOG" \
    || die "listing issues in $BOARD_REPO failed"
  TOTAL_ISSUES=$(wc -l < "$ISSUE_NUMBERS_FILE" | tr -d ' ')
  [ -n "$TOTAL_ISSUES" ] && [ "$TOTAL_ISSUES" -gt 0 ] || die "repos/$BOARD_REPO/issues returned 0 issues"

  START_TS=$(date +%s)
  FETCHED=0
  BUDGET_HIT=0
  FETCH_ERROR=""
  while read -r n; do
    [ -n "$n" ] || continue
    PART="$PARTS_DIR/$n.json"
    if [ -s "$PART" ]; then
      FETCHED=$((FETCHED + 1))
      continue
    fi
    NOW_TS=$(date +%s)
    if [ $((NOW_TS - START_TS)) -ge "$COMMENTS_BUDGET_SECS" ]; then
      BUDGET_HIT=1
      break
    fi
    # Gate review 2026-08-30 (P1): comments are ENRICHMENT on top of the
    # board. A `die` here aborted before manifest.json and before the
    # encrypted offsite step, so one flaky comment call cost the whole day's
    # items/fields/views backup its integrity manifest AND its offsite copy.
    # Degrade instead: record the failure, stop fetching, ship a "partial"
    # comments.json, and let the rest of the export complete.
    if ! gh api -X GET "repos/$BOARD_REPO/issues/$n/comments" --paginate -f per_page=100 \
        --jq '.[] | {id, author: .user.login, createdAt: .created_at, body}' \
        > "$PART.tmp" 2>>"$LOG"; then
      rm -f "$PART.tmp"
      FETCH_ERROR="fetching comments for $BOARD_REPO#$n failed (see $LOG)"
      echo "gh-board-export: WARNING $FETCH_ERROR; stopping comments capture at $FETCHED/$TOTAL_ISSUES" | tee -a "$LOG"
      break
    fi
    if [ -s "$PART.tmp" ]; then
      python3 -c "
import json, sys
for line in open(sys.argv[1]):
    line = line.strip()
    if line:
        json.loads(line)
" "$PART.tmp" || die "comments for $BOARD_REPO#$n are not valid JSON lines"
    fi
    mv "$PART.tmp" "$PART"
    FETCHED=$((FETCHED + 1))
    sleep "$COMMENTS_PACE"
  done < "$ISSUE_NUMBERS_FILE"
  rm -f "$ISSUE_NUMBERS_FILE"

  python3 - "$PARTS_DIR" "$DEST/comments.json.tmp" "$BOARD_REPO" "$TOTAL_ISSUES" "$FETCHED" "$BUDGET_HIT" "$FETCH_ERROR" <<'PY' \
    || die "cannot assemble comments.json"
import json, os, sys
parts_dir, out_path, repo, total, fetched, budget_hit, fetch_error = (
    sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]),
    sys.argv[6] == "1", sys.argv[7])
issues = {}
comment_total = 0
for fn in sorted(os.listdir(parts_dir)):
    if not fn.endswith(".json"):
        continue
    number = fn[:-5]
    rows = []
    with open(os.path.join(parts_dir, fn)) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    issues[number] = rows
    comment_total += len(rows)
out = {
    "repo": repo,
    "status": ("partial (wall-clock budget hit, resumes next run)" if budget_hit
               else "partial (%s; resumes next run)" % fetch_error if fetch_error
               else "complete"),
    "issues_captured": fetched,
    "issues_total": total,
    "comment_total": comment_total,
    "issues": issues,
}
json.dump(out, open(out_path, "w"), indent=2)
PY
  [ -s "$DEST/comments.json.tmp" ] || die "comments.json is empty"
  mv "$DEST/comments.json.tmp" "$DEST/comments.json"
  COMMENT_TOTAL=$(python3 -c "import json; print(json.load(open('$DEST/comments.json'))['comment_total'])") \
    || die "cannot count comments.json"
  if [ "$BUDGET_HIT" -eq 1 ]; then
    echo "gh-board-export: WARNING comments capture hit its $COMMENTS_BUDGET_SECS s budget at $FETCHED/$TOTAL_ISSUES issues; resumes next run from $PARTS_DIR" | tee -a "$LOG"
  fi
  echo "gh-board-export: captured $COMMENT_TOTAL comments across $FETCHED/$TOTAL_ISSUES issues in $BOARD_REPO" | tee -a "$LOG"
  COMMENTS_MANIFEST_NOTE=$(python3 -c "
import json
d = json.load(open('$DEST/comments.json'))
print(json.dumps({k: d[k] for k in ('repo', 'status', 'issues_captured', 'issues_total', 'comment_total')}))
")
fi

# ---- 5. manifest.json --------------------------------------------------------------
python3 - "$DEST" "$ITEM_COUNT" "$FIELD_COUNT" "$VIEW_COUNT" "$COMMENTS_MANIFEST_NOTE" <<'PY' > "$DEST/manifest.json" \
  || die "cannot write manifest.json"
import hashlib, json, sys, datetime
dest, item_count, field_count, view_count, comments_note = (
    sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[5])
files = ["items.json", "fields.json", "views.json"]
import os
if os.path.exists(f"{dest}/comments.json"):
    files.append("comments.json")
shas = {}
for f in files:
    h = hashlib.sha256()
    with open(f"{dest}/{f}", "rb") as fh:
        h.update(fh.read())
    shas[f] = h.hexdigest()
manifest = {
    "project": "Jeff - All Projects (github.com/users/bigbrownjeff/projects/1)",
    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "item_count": item_count,
    "field_count": field_count,
    "comments": json.loads(comments_note),
    "view_count": view_count,
    "sha256": shas,
}
print(json.dumps(manifest, indent=2))
PY
[ -s "$DEST/manifest.json" ] || die "manifest.json is empty"
echo "gh-board-export: captured $ITEM_COUNT items, $FIELD_COUNT fields, $VIEW_COUNT views at $DEST" | tee -a "$LOG"

# ---- 6. restore doc, generated not hand-written -------------------------------------
cat > "$VAULT/README.md" <<DOC
# Restoring GitHub Project #1 ("Jeff - All Projects")

Generated by \`~/.claude/bin/gh-board-export.sh\` on every run. Edit the script, not
this file.

**This is a SNAPSHOT BACKUP, not a live mirror.** It captures items/fields/views at
export time. It does not sync continuously, does not merge concurrent edits, and
restoring from it recreates cards as NEW cards (new item ids, new content ids) — it
does not resurrect the originals in place, and it CANNOT un-convert an issue back to a
draft (there is no such mutation; see "Rebuild items" below). Use it to recover from
accidental deletion of the board or catastrophic account loss, not as a version-control
system for day-to-day edits.

## What's captured, and where

- \`items.json\` — every item (\`gh project item-list\`), field values included.
- \`fields.json\` — field/option definitions (\`gh project field-list\`).
- \`views.json\` — each view's name/layout/filter/sortBy/groupBy (GraphQL). If a
  sub-field wasn't queryable on a given run, \`views.json\` carries an \`_export_note\`
  saying so instead of the run failing outright.
- \`comments.json\` — every Issue-backed card's comment thread (REST, one call per
  issue, paced and resumable within a run). Shape: \`{repo, status, issues_captured,
  issues_total, comment_total, issues: {"<issue number>": [{id, author, createdAt,
  body}, ...]}}\`. \`status\` is \`"complete"\` or a \`"partial (...)"\` note naming why
  (a wall-clock budget cutoff resumes from that day's part files on the next run).
  When \`GH_BOARD_REPO=""\` (no board repo configured), this file is NOT written at
  all; \`manifest.json\` carries \`"comments": "skipped (no board repo configured)"\`
  instead — the absence is always recorded, never silent.
- \`manifest.json\` — counts + sha256 of the four files above, so integrity is
  checkable without re-fetching from GitHub.

Local dated copies live in \`$VAULT/<YYYY-MM-DD>/\`, retained $KEEP_DAYS days.
Offsite copy (client-side encrypted before it leaves this machine):
\`$REMOTE/current\` via rclone. Deletions/overwrites offsite are archived to
\`$REMOTE/replaced/<date>\`, never destroyed outright.

## Rebuild items from a snapshot (manual, deliberate)

There is no bulk "restore" command — GitHub Projects has no bulk item-import API, and
**there is no un-convert mutation either**: once a draft becomes an issue it cannot be
turned back, and a restore that recreates a card is a brand-new issue, never a reversal
of that conversion. Per item, from \`items.json\`:

1. Read the item's \`content.type\`. If it is already \`"Issue"\` and its
   \`content.url\` resolves (the content still exists in its repo, most likely
   $BOARD_REPO), skip recreation entirely and go straight to step 3: re-add it to the
   project with \`gh project item-add $NUMBER --owner $OWNER --url <content.url>\`. Only
   a \`"DraftIssue"\`, or an \`"Issue"\` whose repo/content was itself lost, needs
   recreating.
2. Recreate lost content as a real issue in the board repo. **Do not use
   \`gh project item-create\`** — on an Issue-backed board that mints a DraftIssue,
   which is the wrong content type and defeats the whole point of the T-1742
   conversion (comments, @claude mentions, mobile notifications). Instead:
   \`\`\`
   gh issue create --repo "$BOARD_REPO" --title "<content.title>" --body "<content.body>"
   # then attach the new issue to the project (this is what mints the new item id):
   gh project item-add $NUMBER --owner $OWNER --url <url printed above>
   \`\`\`
   The item id returned by \`item-add\` is **brand new**. It is not, and cannot be made
   to be, the original \`PVTI_...\` id — GitHub does not let a caller choose an item id,
   and there is no map from "this used to be item X" built into either command.
3. For each non-default field value (including Ref, so \`board-ref\` does not mint a
   second one), look up the field id in \`fields.json\` and set it:
   \`\`\`
   gh project item-edit --id <new-item-id> --field-id <field-id> --project-id <project-node-id> --text "<value>"
   # or --single-select-option-id <option-id> for Status/Priority-style fields
   \`\`\`
   (get \`<project-node-id>\` via \`gh api graphql\` on \`user(login:"$OWNER"){ projectV2(number:$NUMBER){ id } }\`.)
4. Views (\`views.json\`) are recreated by hand in the GitHub UI — filter/sort/group
   strings are documented there but there is no \`gh\` or GraphQL mutation to create a
   ProjectV2View from a script as of this writing.
5. **Backfill \`crm.db\` \`tasks.board_item_id\`.** Every recreated item's new id breaks
   any row that pointed at the old one (a card is \`board_missing\` from the CRM's point
   of view the moment its stored id stops resolving). Build an old-id -> new-id map from
   \`pre-items.json\` (old side) and \`item-add\`'s output (new side), and **rehearse the
   backfill on a \`/tmp\` copy of \`crm.db\` first** (skill \`dry-run-backfill\`, memory
   \`backup-before-anything-else\`): assert the row count changed equals the map size and
   that no row flips to \`board_missing\`, before writing to the real database. This is a
   production data write and needs Jeff's own word at the time, separate from whatever
   authorized the restore itself (memory \`data-writes-need-explicit-word\`); this script
   does not perform it and does not ask for that word on your behalf.
6. **Replay \`comments.json\` onto the recreated issue, same shape as
   \`~/.claude/bin/board-history-replay\`.** GitHub does not let a caller backdate a
   comment, so this is a replay, not a restore-in-place: for each issue number in
   \`comments.json\`, post its \`issues[<number>]\` array as new comments on the
   **new** issue number from step 2, in the array's existing chronological order
   (\`createdAt\` ascending), one \`gh api repos/$BOARD_REPO/issues/<new-number>/comments
   -f body=<text>\` call per comment, each carrying the original author and timestamp
   in the text itself since the new comment's own \`created_at\` will be the replay
   time, not the original:
   \`\`\`
   [replayed from backup, original author <author>, original stamp <createdAt>]
   <body>
   \`\`\`
   Paced the same as the capture (\$GH_BOARD_COMMENTS_PACE) and idempotent the same
   way \`board-history-replay\` is: append a \`<!-- comments-replayed: N comments -->\`
   marker to the issue body once its replay completes, so a restore that is
   interrupted and rerun does not double-post. Skip any issue whose body already
   carries that marker.

For a handful of items this is a few minutes of manual work; for a full-board rebuild
budget real time, plural hours, not minutes — this format optimizes for "the data
survives," not "one-command undo," and a full rebuild is the only path back after a
board-wide loss (there is no un-convert to fall back on).

## Restore from offsite (laptop loss, new Mac)

    rclone copy $REMOTE/current ~/gh-board-restore
    cat ~/gh-board-restore/manifest.json   # verify sha256 against the files present

Without the rclone config for \`gdw\` / \`gdw-crypt\` the offsite copy is UNREADABLE.
Its password/password2 live in \`~/.config/rclone/rclone.conf\` only obscured
(reversible) — keep a copy in the password manager, NOT in this vault.
DOC

# ---- 7. rotation (local, KEEP_DAYS) --------------------------------------------------
keep_list=$(mktemp) || die "mktemp"
ls -1d "$VAULT"/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]/ 2>/dev/null | sort | tail -n "$KEEP_DAYS" > "$keep_list"
if [ ! -s "$keep_list" ]; then
  echo "gh-board-export: WARNING keep-list empty — skipping rotation entirely" | tee -a "$LOG"
else
  ls -1d "$VAULT"/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]/ 2>/dev/null | while read -r d; do
    grep -qxF "$d" "$keep_list" || { echo "gh-board-export: pruning $(basename "$d")" | tee -a "$LOG"; rm -rf "$d"; }
  done
fi
rm -f "$keep_list"

# ---- 8. encrypted offsite ------------------------------------------------------------
offsite_ok=false
if [ "$OFFSITE" = "1" ]; then
  command -v rclone >/dev/null 2>&1 || die "GH_BOARD_OFFSITE=1 but rclone is not installed"

  rtype=$(rclone config show "${REMOTE%%:*}" 2>/dev/null | sed -n 's/^type = //p')
  [ "$rtype" = "crypt" ] || die "refusing offsite: remote ${REMOTE%%:*} is type '${rtype:-unknown}', not crypt"

  preflight_err=$(rclone lsd "${REMOTE%%:*}:" --max-depth 1 \
        --timeout 15s --contimeout 15s --retries 1 --low-level-retries 1 \
        2>&1 >/dev/null)
  preflight_rc=$?
  [ "$preflight_rc" -eq 0 ] \
    || die "OFFSITE PREFLIGHT FAILED — remote auth or reachability; rclone said: ${preflight_err:-<no output, exit $preflight_rc>}"

  if rclone sync "$VAULT" "$REMOTE/current" \
        --backup-dir "$REMOTE/replaced/$DATE" \
        --exclude '.DS_Store' --transfers 4 --timeout 5m 2>>"$LOG" | tee -a "$LOG"; then
    :
  else
    die "rclone sync to $REMOTE/current"
  fi
  remote_n=$(rclone size "$REMOTE/current" --json 2>/dev/null | sed -n 's/.*"count":\([0-9]*\).*/\1/p')
  [ -n "$remote_n" ] && [ "$remote_n" -gt 0 ] || die "offsite verify: only ${remote_n:-0} objects"
  echo "gh-board-export: verified $remote_n object(s) offsite at $TS" | tee -a "$LOG"
  offsite_ok=true
else
  echo "gh-board-export: offsite DISABLED (GH_BOARD_OFFSITE=0) — local copy only" | tee -a "$LOG"
fi

echo "gh-board-export: ok at $TS (items=$ITEM_COUNT fields=$FIELD_COUNT views=$VIEW_COUNT offsite=$offsite_ok)" | tee -a "$LOG"
