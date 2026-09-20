#!/usr/bin/env python3
"""dangling-tasks.py must never clobber the shared board-cache.json with a
truncated pull.

board-cache.json is written and read by BOTH this lint and ~/.claude/bin/failtask
(same path, same {"v": 2, "items": [...]} shape). failtask dedupes card filing
against it (any-age cache, per its `fetch_items` docstring). Before this fix,
dangling-tasks.py pulled with a hard-coded `--limit 1000` and wrote whatever it
got straight over that shared file with no check against gh's `totalCount` --
so once the board passed 1000 items (it holds ~1,523 as of 2026-09-19),
a dangling-tasks.py run would silently overwrite a full cache with a short one,
blinding failtask's dedupe to every card past the cut and causing it to
re-file duplicates. This is the same bug class failtask's own LIST_LIMIT fix
targeted (ledger 33), just from the other tool that shares its cache file.

Run: python3 -m unittest local-tools/tests/test_dangling_tasks_cache_truncation.py -v
"""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "dangling-tasks.py")

_loader = SourceFileLoader("dangling_tasks", SCRIPT)
_spec = importlib.util.spec_from_loader("dangling_tasks", _loader)
dt = importlib.util.module_from_spec(_spec)
_loader.exec_module(dt)


class _FakeResult:
    def __init__(self, returncode, stdout, stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class BoardCacheTruncationTests(unittest.TestCase):
    def setUp(self):
        self._orig_cache = dt.CACHE
        self._orig_run = dt.subprocess.run
        self._tmp = tempfile.TemporaryDirectory()
        dt.CACHE = os.path.join(self._tmp.name, "board-cache.json")

    def tearDown(self):
        dt.CACHE = self._orig_cache
        dt.subprocess.run = self._orig_run
        self._tmp.cleanup()

    def _seed_cache(self, n):
        items = [{"id": "PVTI_%d" % i} for i in range(n)]
        with open(dt.CACHE, "w") as f:
            json.dump({"v": 2, "items": items}, f)
        return items

    def test_truncated_live_pull_does_not_overwrite_a_fuller_cache(self):
        """The core defect: 1523 real items, a 1000-item pull, one shared file."""
        self._seed_cache(1523)

        def fake_run(cmd, **kwargs):
            truncated = [{"id": "PVTI_%d" % i} for i in range(1000)]
            return _FakeResult(0, json.dumps({"items": truncated, "totalCount": 1523}))

        dt.subprocess.run = fake_run
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            items, src = dt.board_items(use_cache=False)

        with open(dt.CACHE) as f:
            on_disk = json.load(f)["items"]
        self.assertEqual(
            len(on_disk), 1523,
            "board-cache.json (shared with failtask) was overwritten with a "
            "truncated pull even though a fuller cache already existed on disk")
        self.assertEqual(len(items), 1523, "the lint itself should use the fuller list")
        self.assertIn("TRUNCATED", buf.getvalue())

    def test_complete_live_pull_still_writes_the_cache(self):
        """Guard against over-correcting into never writing at all."""

        def fake_run(cmd, **kwargs):
            items = [{"id": "PVTI_%d" % i} for i in range(1523)]
            return _FakeResult(0, json.dumps({"items": items, "totalCount": 1523}))

        dt.subprocess.run = fake_run
        items, src = dt.board_items(use_cache=False)

        self.assertEqual(src, "live")
        self.assertEqual(len(items), 1523)
        with open(dt.CACHE) as f:
            on_disk = json.load(f)["items"]
        self.assertEqual(len(on_disk), 1523)

    def test_list_limit_is_at_least_failtasks_own_fixed_limit(self):
        """Pin the constant, not just the behaviour it enables."""
        self.assertGreaterEqual(int(dt.LIST_LIMIT), 5000)


if __name__ == "__main__":
    unittest.main()
