#!/usr/bin/env python3
"""Nightly sweep: client deliverables/workups/pitches -> Notes Vault (T-1805).

Trigger: the Monica draft (mattel-engagement/docs/deliverables/2026-08-29/
monica-ask-amended.md) was invisible to the vault; see
~/Projects/_hygiene/ecosystem-unification-2026-08-31.md, section L1a.

What it does: walks ~/Projects/* for
  - */docs/deliverables/**  (any repo's dated deliverable drops)
  - */workups/**            (the outbound CRM's account-workup corpus)
  - client-pitches/pitches/**
and logs each file as one idempotent vault note (source_app="deliverable"),
tagged with the owning repo, a per-company tag when applicable, and
"deliverable" so the whole class is findable via #source=deliverable or
#tag=deliverable.

Content handling (T-1807 ruling):
  - .md files land FULL TEXT in the note body, verbatim.
  - Binaries (.pdf .docx .pptx .xlsx .csv) land as excerpt/metadata (filename,
    size, mtime, first row for .csv) plus a "reveal in Finder" link. Stdlib-only
    means we cannot extract text from pdf/docx/pptx/xlsx, so no attempt is made;
    that is a known limitation, not a bug.
  - Every note ends with a footer carrying the source file's sha256 (first 16
    hex chars). Idempotency rides on that: the note's title+tags+body are hashed
    by notes_vault.logger internally, and the file is only rewritten (never
    duplicated -- same note_id every run) when that hash changes, i.e. when the
    source file's content actually changed. An untouched file costs one file
    stat + one read + a no-op write check; nothing about the vault entry moves.

Opt-out (two mechanisms, either is enough to exclude content):
  1. Directory sentinel: drop a file named ".vault-skip" in any directory under
     a scanned root. That directory and everything below it is skipped. Use
     this for a client repo you do not want mirrored into the shared vault.
  2. Per-file frontmatter marker: a .md file whose own YAML frontmatter has a
     "tags:" list containing "no-vault", or a top-level "vault: skip" key, is
     skipped individually. This lets one file opt out of an otherwise-swept
     directory.
  3. External skip-list: ~/.claude/deliverables-sweep-skip.json, a JSON list of
     substrings; any candidate path containing one of them is skipped. Use this
     when you cannot (or should not) drop a marker file into the source repo.

Routed writes (record contract, board card 2410): since 2026-11-23 the vault of
record is the cloud machine and this Mac holds a read-only replica whose
WRITES-FROZEN sentinel says "moved". The sweep is a registered routed generator:
it passes routed=True to require_apply_authority, and notes_vault.logger.add_note
hands each note to the outbox and returns a dict with queued=True (no note id yet,
no local file). Per run:
  - a note whose title+tags+body hash equals the last hash known on the record
    (the outbox's SENT-HASHES.json, or the replica file, whichever is newer) or the
    hash of an item of it still waiting in the outbox's pending/ is SKIPPED before
    add_note, so an unchanged note is never queued again, sent or not yet sent;
  - every other note is counted from add_note's return dict: queued (moved vault)
    or written (local vault);
  - a refused write (secret scan, bad item) is counted as refused, printed to
    stderr, and makes the run exit nonzero; it is never swallowed;
  - ONE outbox.flush() at the end sends the batch (use --no-flush to only queue);
  - the final bulk import_dir only runs on a vault that has NOT moved (the
    replica is read-only and the record indexes its own notes).
On a local (not moved) vault the old path is kept: add_note runs with
importer.import_dir patched to a no-op (add_note would otherwise reimport the whole
log dir per file, quadratic on ~2,000 notes) and one real import_dir pass at the end.

Loud failure: any uncaught exception, any refused note, any per-file error and any
flush transport error exits nonzero and reports through `failtask outbound`, per
CLAUDE.md #4 (loud errors beat silent fallbacks). The last stdout line always
states written, queued, skipped and refused counts.

Run: python3 ~/.claude/bin/deliverables-sweep.py [--dry-run] [--projects-root DIR]
     [--limit N] [--no-flush]
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.parse

VAULT_REPO = os.path.expanduser("~/Projects/notes-vault")
PROJECTS_ROOT_DEFAULT = os.path.expanduser("~/Projects")
SKIP_LIST_PATH = os.path.expanduser("~/.claude/deliverables-sweep-skip.json")
SKIP_SENTINEL = ".vault-skip"

SOURCE_APP = "deliverable"
LOG_DIR_REL = os.path.join("data", "wiki", "deliverable")  # relative to VAULT_REPO

TEXT_EXTS = {".md"}
BINARY_EXTS = {".pdf", ".docx", ".pptx", ".xlsx", ".csv"}
ALL_EXTS = TEXT_EXTS | BINARY_EXTS

SKIP_DIR_NAMES = {".git", "node_modules", "__pycache__"}
SKIP_PATH_SEGMENTS = ("/_wt/", "/.claude/worktrees/", "/ladder/runs/")
SCAN_MARKERS = ("/docs/deliverables/", "/workups/", "/pitches/")

FAILTASK = os.path.expanduser("~/.claude/bin/failtask")
# A rehearsal (NV_REHEARSAL=1) must never file a board card.
REHEARSAL = os.environ.get("NV_REHEARSAL") == "1"


def _load_vault_modules(vault_repo):
    """Import notes_vault from the vault repo checkout. Chdir is required:
    notes_vault paths (data/notes.db, data/wiki/...) are relative to cwd, the
    same convention every scripts/wiki/build_*.py generator relies on."""
    sys.path.insert(0, vault_repo)
    os.chdir(vault_repo)
    sys.path.insert(0, os.path.join(vault_repo, "scripts"))
    from notes_vault.db import connect
    from notes_vault import logger, importer, freeze, outbox
    from notes_vault.frontmatter import parse as parse_frontmatter, FrontmatterError
    try:
        from write_authority import require_apply_authority
    except ImportError:
        require_apply_authority = None
    return (connect, logger, importer, freeze, outbox, parse_frontmatter, FrontmatterError,
            require_apply_authority)


def load_skip_list(path=None):
    path = path or SKIP_LIST_PATH  # resolved at call time, not def time (tests monkeypatch it)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(path + " must contain a JSON list of path substrings")
    return [str(s) for s in data if str(s).strip()]


def slugify(text, fallback="x"):
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return cleaned or fallback


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return (str(n) if unit == "B" else "{:.1f}".format(n)) + " " + unit
        n /= 1024.0
    return str(n)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def md_frontmatter_optout(path, parse_frontmatter, FrontmatterError):
    """True if a .md file's own YAML frontmatter opts it out (mechanism 2)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return False
    if not text.startswith("---"):
        return False
    try:
        fm, _ = parse_frontmatter(text)
    except FrontmatterError:
        return False
    tags = fm.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    if "no-vault" in [str(t).strip().lower() for t in tags]:
        return True
    return str(fm.get("vault", "")).strip().lower() == "skip"


def iter_candidate_files(projects_root, skip_substrings):
    for dirpath, dirnames, filenames in os.walk(projects_root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES
                              and not d.startswith("."))
        norm = dirpath.replace(os.sep, "/") + "/"
        if any(seg in norm for seg in SKIP_PATH_SEGMENTS):
            dirnames[:] = []
            continue
        if SKIP_SENTINEL in filenames:
            dirnames[:] = []  # opt-out mechanism 1: whole subtree excluded
            continue
        if not any(marker in norm for marker in SCAN_MARKERS):
            continue
        for fname in sorted(filenames):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in ALL_EXTS:
                continue
            path = os.path.join(dirpath, fname)
            relpath = os.path.relpath(path, projects_root)
            if any(s in relpath for s in skip_substrings):
                continue
            yield path, relpath


def deliv_id(repo, relpath_after_repo):
    stem = "deliv-" + slugify(repo) + "-" + slugify(relpath_after_repo)
    if len(stem) > 180:
        digest = hashlib.sha1(relpath_after_repo.encode("utf-8")).hexdigest()[:10]
        stem = stem[:160].rstrip("-") + "-" + digest
    return stem


def split_repo(relpath):
    """relpath is relative to ~/Projects. Returns (repo, relpath_after_repo)."""
    parts = relpath.split(os.sep)
    return parts[0], os.sep.join(parts[1:])


def company_tag(rel_after_repo):
    parts = rel_after_repo.split(os.sep)
    for marker in ("workups", "pitches"):
        if marker in parts:
            idx = parts.index(marker)
            if idx + 1 < len(parts):
                return slugify(parts[idx + 1])
    return None


def build_title(repo, rel_after_repo, path):
    parts = rel_after_repo.split(os.sep)
    base = os.path.basename(path)
    m = re.search(r"(20\d{2}-\d{2}-\d{2})", rel_after_repo)
    date = m.group(1) if m else time.strftime(
        "%Y-%m-%d", time.localtime(os.path.getmtime(path)))
    if "workups" in parts:
        company = company_tag(rel_after_repo) or repo
        return company + " workup file: " + base + " (" + date + ")"
    if "pitches" in parts:
        company = company_tag(rel_after_repo) or repo
        return company + " pitch package file: " + base + " (" + date + ")"
    return repo + " deliverable: " + base + " (" + date + ")"


def build_body(path, repo, sha, size, is_text):
    dirname = os.path.dirname(path)
    base = os.path.basename(path)
    link = "[Open folder in Finder](/open?path=" + urllib.parse.quote(dirname, safe="") + ")"
    footer = ("\n\n---\nFile: `" + base + "`\nFolder: `" + dirname + "`\n" + link
              + "\nRepo: `" + repo + "`\nsha256: `" + sha[:16] + "`\nsize: "
              + human_size(size))
    if is_text:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        return text.rstrip("\n") + footer
    first_row = ""
    if path.lower().endswith(".csv"):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                first_row = fh.readline().strip()
        except OSError:
            first_row = ""
    ext = os.path.splitext(base)[1].lstrip(".").upper()
    excerpt = ("Binary deliverable (" + ext + "). Full text is not indexed "
               "(stdlib-only tooling cannot parse " + ext + "); open the file "
               "directly.")
    if first_row:
        excerpt += "\nFirst row: `" + first_row[:200] + "`"
    return excerpt + footer


def _pending_hashes(outbox):
    """{note_id: content hash} of the sweep notes queued in the outbox and not yet
    sent. Newest item per note wins; a pending append voids the note's entry (its
    content on the record is no longer the replace's). Read-only, and an item that
    cannot be read is ignored (flush will refuse it; the sweep must not die on it)."""
    root = outbox.outbox_root()
    rows = []
    for ev in outbox._list_ids(root, "pending"):
        d = os.path.join(root, "pending", ev)
        try:
            item = outbox.items.read_item(d)
            if item.get("source_app") != SOURCE_APP:
                continue
            origin = item["origin"]
            rows.append((origin["host"], origin["seq"], item, d))
        except (outbox.items.FramingError, KeyError, TypeError, ValueError, OSError):
            continue
    rows.sort(key=lambda r: (r[0], r[1]))
    hashes = {}
    for _host, _seq, item, d in rows:
        if item.get("op") == "append":
            hashes.pop(item["source_id"], None)
            continue
        try:
            hashes[item["source_id"]] = outbox._item_hash(item, d)
        except (KeyError, OSError):
            continue
    return hashes


def _known_hash(vault_moved, outbox, logger, parse_frontmatter, data_dir, log_dir, note_id,
                pending=None):
    """The content hash of the note as last known to the record, or None.

    Moved vault: a note already queued in the outbox (pending, newest), else the newer
    of the outbox's SENT-HASHES entry and the replica file (outbox._base_hash, the same
    value the outbox stamps as base_hash on a replace).
    Local vault: the note file already in log_dir."""
    if vault_moved:
        if pending and note_id in pending:
            return pending[note_id]
        return outbox._base_hash(outbox.outbox_root(), data_dir,
                                 outbox._norm_log_dir(log_dir, SOURCE_APP),
                                 SOURCE_APP, note_id)
    path = os.path.join(log_dir, note_id + ".md")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            fm, body = parse_frontmatter(fh.read())
    except (OSError, ValueError):
        return None
    return logger._content_hash(fm.get("title"), fm.get("tags"), body)


def run_sweep(projects_root=PROJECTS_ROOT_DEFAULT, vault_repo=VAULT_REPO, dry_run=False,
              db_path=None, log_dir=None, limit=None, flush=True):
    (connect, logger, importer, freeze, outbox, parse_frontmatter, FrontmatterError,
     require_apply_authority) = _load_vault_modules(vault_repo)

    # Absolute overrides let tests and one-off runs point at a throwaway
    # vault (write_authority's is_production_database() waves anything under
    # a system temp dir through without touching the real notes.db or the
    # notes-vault checkout's git state).
    db_path = db_path or os.path.join(vault_repo, "data", "notes.db")
    log_dir = log_dir or os.path.join(vault_repo, LOG_DIR_REL)
    if require_apply_authority is not None and not dry_run:
        # routed=True: a moved vault is allowed, and the authority is published in
        # NV_ROUTED_AUTHORITY for the outbox (registered generator, contract 2.2).
        require_apply_authority(tool="deliverables-sweep.py", db_path=db_path, routed=True)

    data_dir = freeze.data_dir_for_db(db_path)
    vault_moved = freeze.routing_target(freeze.read_state(data_dir)) is not None

    skip_substrings = load_skip_list()
    seen = written = queued = skipped = skipped_optout = refused = errors = 0

    real_import_dir = importer.import_dir

    def _noop_import_dir(conn, source_dir, source_app="evernote"):
        return {"found": 0, "inserted": 0, "updated": 0, "attachments": 0,
                "missing_attachments": 0, "parse_errors": [], "parse_error_count": 0}

    pending = _pending_hashes(outbox) if (vault_moved and not dry_run) else {}
    conn = None
    if not dry_run:
        if vault_moved:
            # add_note only reads the data directory off this handle before it routes
            # the call to the outbox. An immutable handle cannot write a WAL side file
            # into the read-only replica tree (what connect() could do).
            conn = sqlite3.connect("file:" + os.path.realpath(db_path) + "?immutable=1",
                                   uri=True)
        else:
            conn = connect(db_path)
    patched = False
    try:
        if not dry_run and not vault_moved:
            importer.import_dir = _noop_import_dir  # deferred bulk reimport; see docstring
            patched = True
        for path, relpath in iter_candidate_files(projects_root, skip_substrings):
            if limit is not None and seen >= limit:
                break
            seen += 1
            repo, rel_after_repo = split_repo(relpath)
            ext = os.path.splitext(path)[1].lower()
            is_text = ext in TEXT_EXTS
            try:
                if is_text and md_frontmatter_optout(path, parse_frontmatter, FrontmatterError):
                    skipped_optout += 1
                    continue
                st = os.stat(path)
                sha = sha256_file(path)
                title = build_title(repo, rel_after_repo, path)
                body = build_body(path, repo, sha, st.st_size, is_text)
                tags = [slugify(repo), "deliverable"]
                ctag = company_tag(rel_after_repo)
                if ctag and ctag not in tags:
                    tags.append(ctag)
                note_id = deliv_id(repo, rel_after_repo)
                if dry_run:
                    print("DRY-RUN would log:", note_id, "<-", relpath)
                    written += 1
                    continue
                if _known_hash(vault_moved, outbox, logger, parse_frontmatter, data_dir,
                               log_dir, note_id, pending) == logger._content_hash(title, tags, body):
                    skipped += 1  # unchanged since the record last took it, or already queued
                    continue
                result = logger.add_note(
                    conn, title=title, body=body, tags=tags, source="claude.sweep",
                    source_app=SOURCE_APP, log_dir=log_dir, note_id=note_id)
                if result.get("queued"):
                    queued += 1
                else:
                    written += 1
            except outbox.OutboxRefusal as exc:
                refused += 1
                print("REFUSED", path, ":", exc, file=sys.stderr)
            except Exception:
                errors += 1
                print("ERROR on", path, ":", traceback.format_exc(), file=sys.stderr)
    finally:
        if patched:
            importer.import_dir = real_import_dir

    flush_stats = {"sent": 0, "refused": 0, "deferred": 0, "left": 0, "paused": False,
                   "busy": False, "error": None}
    import_stats = {"found": 0, "inserted": 0, "updated": 0}
    if not dry_run and conn is not None:
        if vault_moved:
            if flush and queued:
                flush_stats = outbox.flush()
        else:
            os.makedirs(log_dir, exist_ok=True)  # nothing may have been written this run
            import_stats = real_import_dir(conn, log_dir, source_app=SOURCE_APP)
        conn.close()

    return {
        "seen": seen,
        "written": written,
        "queued": queued,
        "skipped": skipped,
        "skipped_optout": skipped_optout,
        "refused": refused,
        "errors": errors,
        "moved": vault_moved,
        "flush": flush_stats,
        "import": import_stats,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--projects-root", default=PROJECTS_ROOT_DEFAULT)
    ap.add_argument("--vault-repo", default=VAULT_REPO)
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N candidate files (rehearsal)")
    ap.add_argument("--no-flush", action="store_true",
                    help="queue only; leave the send to the outbox job")
    args = ap.parse_args(argv)

    try:
        stats = run_sweep(projects_root=args.projects_root, vault_repo=args.vault_repo,
                           dry_run=args.dry_run, limit=args.limit, flush=not args.no_flush)
    except Exception as exc:
        detail = traceback.format_exc()
        print("deliverables-sweep FAILED:", exc, file=sys.stderr)
        print(detail, file=sys.stderr)
        if os.path.isfile(FAILTASK) and not REHEARSAL:
            subprocess.run([FAILTASK, "outbound", "deliverables sweep failed",
                             "--detail", str(exc)[:500]], check=False)
        return 1

    fl = stats["flush"]
    problems = []
    if stats["refused"] or fl["refused"]:
        problems.append("%d refused (%d at queue, %d by the record)" % (
            stats["refused"] + fl["refused"], stats["refused"], fl["refused"]))
    if stats["errors"]:
        problems.append("%d per-file error(s)" % stats["errors"])
    if fl["error"]:
        problems.append("flush error: " + str(fl["error"]))
    if fl["paused"]:
        print("WARNING: the outbox is PAUSED; %d note(s) queued, none sent" % stats["queued"],
              file=sys.stderr)
    if fl["busy"]:
        print("WARNING: another flush holds the outbox lock; the queued notes go with it "
              "or on the next 5 minute outbox run", file=sys.stderr)
    if fl["deferred"]:
        print("WARNING: %d note(s) deferred by the record; they stay queued" % fl["deferred"],
              file=sys.stderr)
    for line in problems:
        print("deliverables-sweep PROBLEM:", line, file=sys.stderr)

    print("deliverables-sweep: files_seen=%d written=%d queued=%d skipped=%d refused=%d "
          "errors=%d skipped_optout=%d moved=%s flush_sent=%d flush_refused=%d flush_left=%d "
          "import_found=%d import_inserted=%d import_updated=%d" % (
              stats["seen"], stats["written"], stats["queued"], stats["skipped"],
              stats["refused"] + fl["refused"], stats["errors"], stats["skipped_optout"],
              str(stats["moved"]).lower(), fl["sent"], fl["refused"], fl["left"],
              stats["import"].get("found", 0), stats["import"].get("inserted", 0),
              stats["import"].get("updated", 0)))
    if problems and os.path.isfile(FAILTASK) and not REHEARSAL:
        subprocess.run([FAILTASK, "outbound", "deliverables sweep had refused or failed notes",
                         "--detail", "; ".join(problems)[:500]], check=False)
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
