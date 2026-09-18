"""Unit tests for local-tools/log-rotate.py (stdlib only).

Every test operates on files inside a tempfile.TemporaryDirectory() and never
touches /opt/homebrew/var/log/ollama.log or any other real path.

Run: python3 -m unittest local-tools/tests/test_log_rotate.py -v
"""

import datetime
import gzip
import importlib.util
import os
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "log-rotate.py")

_loader = SourceFileLoader("log_rotate", SCRIPT)
_spec = importlib.util.spec_from_loader("log_rotate", _loader)
lr = importlib.util.module_from_spec(_spec)
_loader.exec_module(lr)


def make_target(path, max_bytes=100, keep=3):
    return {"path": path, "max_bytes": max_bytes, "keep": keep}


def write_bytes(path, n):
    with open(path, "wb") as f:
        f.write(os.urandom(n))


class LogRotateTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmpdir.name, "grower.log")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_under_max_bytes_is_untouched(self):
        write_bytes(self.path, 50)
        before = os.path.getsize(self.path)
        before_ino = os.stat(self.path).st_ino
        messages, errors = lr.rotate_target(make_target(self.path, max_bytes=100), apply=True)
        self.assertEqual(errors, [])
        self.assertTrue(any("no rotation needed" in m for m in messages))
        self.assertEqual(os.path.getsize(self.path), before)
        self.assertEqual(os.stat(self.path).st_ino, before_ino)
        self.assertEqual(lr.find_generations(self.path), [])

    def test_over_max_bytes_is_gzipped_and_truncated_same_inode(self):
        original = os.urandom(200)
        with open(self.path, "wb") as f:
            f.write(original)
        before_ino = os.stat(self.path).st_ino

        messages, errors = lr.rotate_target(make_target(self.path, max_bytes=100), apply=True)

        self.assertEqual(errors, [])
        self.assertEqual(os.path.getsize(self.path), 0, "live log must be truncated to zero")
        self.assertEqual(os.stat(self.path).st_ino, before_ino, "truncate-in-place must keep the same inode")

        generations = lr.find_generations(self.path)
        self.assertEqual(len(generations), 1)
        with gzip.open(generations[0], "rb") as gz:
            roundtripped = gz.read()
        self.assertEqual(roundtripped, original, "gzip generation must round-trip to the original bytes")

    def test_unreadable_generation_aborts_before_the_live_log_is_truncated(self):
        """A generation that does not read back must cost us nothing.

        Truncating is irreversible, so a gzip cut short by a full disk or a
        killed process must abort the rotation with the live bytes still there.
        """
        original = os.urandom(200)
        with open(self.path, "wb") as f:
            f.write(original)

        def short_copy(path, dest):
            # A real gzip header and some payload, then nothing: exactly what a
            # write that died partway through leaves behind.
            with gzip.open(dest, "wb") as gz:
                gz.write(original[:10])
            with open(dest, "r+b") as fh:
                fh.truncate(os.path.getsize(dest) - 4)

        real_copy = lr._gzip_copy
        lr._gzip_copy = short_copy
        try:
            messages, errors = lr.rotate_target(make_target(self.path, max_bytes=100), apply=True)
        finally:
            lr._gzip_copy = real_copy

        self.assertTrue(errors, "a generation that will not read back must be an error")
        self.assertIn("failed to rotate", errors[0])
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), original, "the live log must still hold every byte")

    def test_verify_generation_rejects_a_short_generation(self):
        dest = os.path.join(self.tmpdir.name, "gen.gz")
        with gzip.open(dest, "wb") as gz:
            gz.write(b"x" * 10)
        self.assertEqual(lr._verify_generation(dest, 10), 10)
        with self.assertRaises(OSError) as ctx:
            lr._verify_generation(dest, 11)
        self.assertIn("fewer than the 11 copied", str(ctx.exception))

    def test_mutation_proof_rotation_without_truncate_leaves_data(self):
        """If truncation were ever removed from rotate_target, this goes red.

        Directly proves the behavior the tool exists for: after a rotation
        the live file must be empty, not merely "a gzip generation exists".
        """
        write_bytes(self.path, 200)
        lr.rotate_target(make_target(self.path, max_bytes=100), apply=True)
        self.assertEqual(os.path.getsize(self.path), 0)

    def test_generations_beyond_keep_are_removed_within_keep_are_not(self):
        target = make_target(self.path, max_bytes=100, keep=3)
        base = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        # Rotate 5 times on 5 distinct days -> 5 distinct, sortable stamps.
        for day in range(5):
            write_bytes(self.path, 200)
            now = base + datetime.timedelta(days=day)
            lr.rotate_target(target, apply=True, now=now)

        generations = lr.find_generations(self.path)
        self.assertEqual(len(generations), 3, "only `keep` generations should remain")

        expected_stamps = [
            (base + datetime.timedelta(days=d)).strftime(lr._STAMP_FORMAT) for d in (2, 3, 4)
        ]
        for stamp, gen in zip(expected_stamps, generations):
            self.assertIn(stamp, gen, "the two oldest generations must be the ones removed")

    def test_dry_run_writes_nothing(self):
        write_bytes(self.path, 200)
        before = os.path.getsize(self.path)
        before_mtime = os.path.getmtime(self.path)

        messages, errors = lr.rotate_target(make_target(self.path, max_bytes=100), apply=False)

        self.assertEqual(errors, [])
        self.assertTrue(any("DRY-RUN" in m for m in messages))
        self.assertEqual(os.path.getsize(self.path), before, "dry-run must not truncate")
        self.assertEqual(os.path.getmtime(self.path), before_mtime, "dry-run must not touch the live file")
        self.assertEqual(lr.find_generations(self.path), [], "dry-run must not create a gzip generation")

    def test_dry_run_previews_pruning_without_deleting(self):
        target = make_target(self.path, max_bytes=100, keep=1)
        base = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        write_bytes(self.path, 200)
        lr.rotate_target(target, apply=True, now=base)
        self.assertEqual(len(lr.find_generations(self.path)), 1)

        write_bytes(self.path, 200)
        messages, errors = lr.rotate_target(
            target, apply=False, now=base + datetime.timedelta(days=1)
        )
        self.assertEqual(errors, [])
        self.assertTrue(any("delete generation" in m and "DRY-RUN" in m for m in messages))
        # Nothing was actually removed or written.
        self.assertEqual(len(lr.find_generations(self.path)), 1)
        self.assertEqual(os.path.getsize(self.path), 200)

    def test_missing_file_is_skipped_not_an_error(self):
        missing = os.path.join(self.tmpdir.name, "does-not-exist.log")
        messages, errors = lr.rotate_target(make_target(missing), apply=True)
        self.assertEqual(errors, [])
        self.assertTrue(any("SKIP" in m for m in messages))

    def test_cli_main_dry_run_exits_zero_and_writes_nothing(self):
        write_bytes(self.path, 200)
        original_targets = lr.TARGETS
        try:
            lr.TARGETS = [make_target(self.path, max_bytes=100, keep=3)]
            code = lr.main(["--dry-run"])
        finally:
            lr.TARGETS = original_targets
        self.assertEqual(code, 0)
        self.assertEqual(os.path.getsize(self.path), 200)
        self.assertEqual(lr.find_generations(self.path), [])

    def test_cli_main_apply_rotates(self):
        write_bytes(self.path, 200)
        original_targets = lr.TARGETS
        try:
            lr.TARGETS = [make_target(self.path, max_bytes=100, keep=3)]
            code = lr.main(["--apply"])
        finally:
            lr.TARGETS = original_targets
        self.assertEqual(code, 0)
        self.assertEqual(os.path.getsize(self.path), 0)
        self.assertEqual(len(lr.find_generations(self.path)), 1)


if __name__ == "__main__":
    unittest.main()
