#!/usr/bin/env python3
"""Rotate-with-retention for logs that grow without bound.

Targets are data, not logic: add a row to TARGETS to cover a second grower
without touching the rotation code below.

Rotation strategy is copy-truncate, not rename-and-recreate: the daemon
writing the log (e.g. ollama, via brew services) holds the file open by
path/inode and we cannot restart it from here. A rename would leave the
daemon writing to a now-unlinked inode that `ls` can no longer see, silently
losing every log line until the next manual restart. Instead we gzip a copy
of the current bytes out to a dated generation file, then truncate the
*original* file in place (`open(path, "r+b"); f.truncate(0)`), which keeps
the same inode and path so the daemon's already-open file handle keeps
landing in the same (now empty) file.

Default is dry-run: nothing is written unless --apply is passed. Scheduled
jobs must never swallow stderr, so any failure here prints to stderr and
exits nonzero.
"""

import argparse
import datetime
import gzip
import os
import re
import shutil
import sys
from typing import Dict, List, Tuple

# path: file to watch.
# max_bytes: rotate when the live file exceeds this size.
# keep: how many gzipped generations to retain (oldest beyond this are deleted).
TARGETS: List[Dict] = [
    {
        "path": "/opt/homebrew/var/log/ollama.log",
        "max_bytes": 64 * 1024 * 1024,  # 64 MB
        "keep": 6,  # ~a year of history at the observed growth rate
    },
]

_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_STAMP_RE = r"\d{8}T\d{6}Z"


def _gzip_copy(path: str, dest: str) -> None:
    with open(path, "rb") as src, gzip.open(dest, "wb") as dst:
        shutil.copyfileobj(src, dst)


def _truncate_in_place(path: str) -> None:
    # r+b on an existing path preserves the inode; this is the whole point
    # of copy-truncate over rename (see module docstring).
    with open(path, "r+b") as f:
        f.truncate(0)


def find_generations(path: str) -> List[str]:
    """Existing gzipped generations for path, oldest first (stamp is lexically sortable)."""
    directory = os.path.dirname(path) or "."
    base = os.path.basename(path)
    pattern = re.compile(r"^" + re.escape(base) + r"\." + _STAMP_RE + r"\.gz$")
    if not os.path.isdir(directory):
        return []
    matches = [f for f in os.listdir(directory) if pattern.match(f)]
    matches.sort()
    return [os.path.join(directory, f) for f in matches]


def rotate_target(target: Dict, apply: bool, now: datetime.datetime = None) -> Tuple[List[str], List[str]]:
    """Return (messages, errors). Writes nothing unless apply is True."""
    path = target["path"]
    max_bytes = target["max_bytes"]
    keep = target["keep"]
    messages: List[str] = []
    errors: List[str] = []
    mode = "APPLY" if apply else "DRY-RUN"

    if not os.path.isfile(path):
        messages.append(f"SKIP {path}: not found")
        return messages, errors

    size = os.path.getsize(path)
    will_rotate = size > max_bytes
    stamp = (now or datetime.datetime.now(datetime.timezone.utc)).strftime(_STAMP_FORMAT)
    new_generation = f"{path}.{stamp}.gz"

    if will_rotate:
        messages.append(
            f"{mode} rotate {path} ({size} bytes > {max_bytes} bytes): "
            f"gzip copy -> {new_generation}, then truncate {path} to 0 bytes in place"
        )
        if apply:
            try:
                _gzip_copy(path, new_generation)
                _truncate_in_place(path)
            except OSError as exc:
                errors.append(f"failed to rotate {path}: {exc}")
                return messages, errors
    else:
        messages.append(f"OK {path} ({size} bytes <= {max_bytes} bytes): no rotation needed")

    generations = find_generations(path)
    if will_rotate and not apply and new_generation not in generations:
        # Dry-run preview: show what pruning would do as if rotation had run.
        generations = sorted(generations + [new_generation])

    if len(generations) > keep:
        # Only ever the oldest excess, never anything we have not just
        # proven is beyond `keep` by this same count.
        to_delete = generations[: len(generations) - keep]
        for gen in to_delete:
            messages.append(f"{mode} delete generation {gen} (beyond keep={keep})")
            if apply and os.path.exists(gen):
                try:
                    os.remove(gen)
                except OSError as exc:
                    errors.append(f"failed to delete {gen}: {exc}")

    return messages, errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write: gzip + truncate + prune. Without this flag nothing is written.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit dry-run (this is also the default with no flags).",
    )
    args = parser.parse_args(argv)
    apply = bool(args.apply) and not args.dry_run

    exit_code = 0
    for target in TARGETS:
        try:
            messages, errors = rotate_target(target, apply=apply)
        except Exception as exc:  # loud, never swallowed
            print(f"error: unexpected failure rotating {target.get('path')}: {exc}", file=sys.stderr)
            exit_code = 1
            continue
        for message in messages:
            print(message)
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
