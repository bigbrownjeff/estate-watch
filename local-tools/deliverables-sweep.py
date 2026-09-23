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

Performance note: notes_vault.logger.add_note() reimports its whole log_dir on
every call (by design, for the small corpora -- ~100 notes -- the existing wiki
generators run over). This sweep's corpus is ~10x that, so calling add_note
per file unmodified would reimport an ever-growing directory on every single
file, i.e. quadratic cost that gets worse forever. Instead we call add_note
with notes_vault.importer.import_dir patched to a no-op for the duration of the
sweep (every other add_note code path -- frontmatter, hashing, attachment
copy -- runs untouched) and do exactly one real import_dir pass at the end.
This is the standard sweep-vs-generator size distinction, not a shortcut around
the tested write path.

Loud failure: any uncaught exception exits nonzero and reports through
`failtask outbound`, per CLAUDE.md #4 (loud errors beat silent fallbacks).

Run: python3 ~/.claude/bin/deliverables-sweep.py [--dry-run] [--projects-root DIR]
"""
import argparse
import hashlib
import json
import os
import re
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
SKIP_PATH_SEGMENTS = ("/_wt/", "/.claude/worktrees/")
SCAN_MARKERS = ("/docs/deliverables/", "/workups/", "/pitches/")

FAILTASK = os.path.expanduser("~/.claude/bin/failtask")


def _load_vault_modules(vault_repo):
    """Import notes_vault from the vault repo checkout. Chdir is required:
    notes_vault paths (data/notes.db, data/wiki/...) are relative to cwd, the
    same convention every scripts/wiki/build_*.py generator relies on."""
    sys.path.insert(0, vault_repo)
    os.chdir(vault_repo)
    sys.path.insert(0, os.path.join(vault_repo, "scripts"))
    from notes_vault.db import connect
    from notes_vault import logger, importer
    from notes_vault.frontmatter import parse as parse_frontmatter, FrontmatterError
    try:
        from write_authority import require_apply_authority
    except ImportError:
        require_apply_authority = None
    return connect, logger, importer, parse_frontmatter, FrontmatterError, require_apply_authority


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


def run_sweep(projects_root=PROJECTS_ROOT_DEFAULT, vault_repo=VAULT_REPO, dry_run=False,
              db_path=None, log_dir=None):
    (connect, logger, importer, parse_frontmatter, FrontmatterError,
     require_apply_authority) = _load_vault_modules(vault_repo)

    # Absolute overrides let tests and one-off runs point at a throwaway
    # vault (write_authority's is_production_database() waves anything under
    # a system temp dir through without touching the real notes.db or the
    # notes-vault checkout's git state).
    db_path = db_path or os.path.join(vault_repo, "data", "notes.db")
    log_dir = log_dir or os.path.join(vault_repo, LOG_DIR_REL)
    if require_apply_authority is not None and not dry_run:
        require_apply_authority(tool="deliverables-sweep.py", db_path=db_path)

    skip_substrings = load_skip_list()
    seen = written = unchanged = skipped_optout = errors = 0

    real_import_dir = importer.import_dir

    def _noop_import_dir(conn, source_dir, source_app="evernote"):
        return {"found": 0, "inserted": 0, "updated": 0, "attachments": 0,
                "missing_attachments": 0, "parse_errors": [], "parse_error_count": 0}

    conn = None if dry_run else connect(db_path)
    try:
        if not dry_run:
            importer.import_dir = _noop_import_dir  # deferred bulk reimport; see docstring
        for path, relpath in iter_candidate_files(projects_root, skip_substrings):
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
                target_md = os.path.join(log_dir, note_id + ".md")
                before_mtime = (os.path.getmtime(target_md)
                                 if os.path.isfile(target_md) else None)
                logger.add_note(
                    conn, title=title, body=body, tags=tags, source="claude.sweep",
                    source_app=SOURCE_APP, log_dir=log_dir, note_id=note_id)
                after_mtime = (os.path.getmtime(target_md)
                               if os.path.isfile(target_md) else None)
                # add_note skips its own file write when the hashed content
                # (title+tags+body) is unchanged from the note already on disk;
                # an unmoved mtime is how we detect that from the outside, since
                # the no-op import_dir patch above means the return dict's "id"
                # lookup never resolves during this loop.
                if before_mtime != after_mtime:
                    written += 1
                else:
                    unchanged += 1
            except Exception:
                errors += 1
                print("ERROR on", path, ":", traceback.format_exc(), file=sys.stderr)
    finally:
        if not dry_run:
            importer.import_dir = real_import_dir

    import_stats = {"found": 0, "inserted": 0, "updated": 0}
    if not dry_run and conn is not None:
        os.makedirs(log_dir, exist_ok=True)  # nothing may have been written this run
        import_stats = real_import_dir(conn, log_dir, source_app=SOURCE_APP)
        conn.close()

    return {
        "seen": seen,
        "written": written,
        "unchanged": unchanged,
        "skipped_optout": skipped_optout,
        "errors": errors,
        "import": import_stats,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--projects-root", default=PROJECTS_ROOT_DEFAULT)
    ap.add_argument("--vault-repo", default=VAULT_REPO)
    args = ap.parse_args(argv)

    try:
        stats = run_sweep(projects_root=args.projects_root, vault_repo=args.vault_repo,
                           dry_run=args.dry_run)
    except Exception as exc:
        detail = traceback.format_exc()
        print("deliverables-sweep FAILED:", exc, file=sys.stderr)
        print(detail, file=sys.stderr)
        if os.path.isfile(FAILTASK):
            subprocess.run([FAILTASK, "outbound", "deliverables sweep failed",
                             "--detail", str(exc)[:500]], check=False)
        return 1

    print("deliverables-sweep: files_seen=%d written=%d unchanged=%d skipped_optout=%d "
          "errors=%d import_found=%d import_inserted=%d import_updated=%d" % (
              stats["seen"], stats["written"], stats["unchanged"], stats["skipped_optout"],
              stats["errors"], stats["import"].get("found", 0),
              stats["import"].get("inserted", 0), stats["import"].get("updated", 0)))
    if stats["errors"] and os.path.isfile(FAILTASK):
        subprocess.run([FAILTASK, "outbound", "deliverables sweep had per-file errors",
                         "--detail", str(stats["errors"]) + " file(s) failed; see log"],
                        check=False)
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
