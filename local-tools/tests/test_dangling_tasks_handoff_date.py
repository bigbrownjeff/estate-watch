#!/usr/bin/env python3
"""`handoff_date` returns a POSIX timestamp, and every caller must treat it as one.

The `--file` path rendered it with `ho_date.date()`, which raises
`AttributeError: 'float' object has no attribute 'date'` on the first card it
tries to file. The crash is invisible in the default report-only run, so the
lint looked healthy right up until someone asked it to file.

Run: python3 -m unittest local-tools/tests/test_dangling_tasks_handoff_date.py -v
"""

import datetime
import importlib.util
import os
import re
import tempfile
import unittest
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "dangling-tasks.py")

_loader = SourceFileLoader("dangling_tasks", SCRIPT)
_spec = importlib.util.spec_from_loader("dangling_tasks", _loader)
dt = importlib.util.module_from_spec(_spec)
_loader.exec_module(dt)


class HandoffDateContractTests(unittest.TestCase):
    def test_returns_a_posix_timestamp_not_a_datetime(self):
        """The contract both callers depend on, pinned by type.

        Line 168 compares the result against a cutoff with `>=`, which only
        works on a number. Anything that makes this return a datetime breaks
        that comparison instead, so the type is the real contract here.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "2026-09-14-0615-groundskeeper-walk.md")
            open(path, "w").close()
            got = dt.handoff_date(path)
        self.assertIsInstance(got, float)
        self.assertNotIsInstance(got, datetime.datetime)

    def test_the_filename_date_wins_over_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "2026-06-10-1200-rvc-handoff.md")
            open(path, "w").close()
            stamp = dt.handoff_date(path)
        self.assertEqual(
            datetime.datetime.fromtimestamp(stamp).date().isoformat(), "2026-06-10")

    def test_rendering_a_timestamp_as_a_date_string(self):
        """The exact expression the --file path uses, exercised on its own.

        Restore `ho_date.date().isoformat()` in dangling-tasks.py and this goes
        red with AttributeError, which is what the lint did in production.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "2026-08-28-0900-session.md")
            open(path, "w").close()
            ho_date = dt.handoff_date(path)
        rendered = (datetime.datetime.fromtimestamp(ho_date).date().isoformat()
                    if ho_date else "unknown")
        self.assertEqual(rendered, "2026-08-28")

    def test_no_caller_treats_the_result_as_a_datetime(self):
        """Source-level guard: `handoff_date(...)` is never followed by `.date()`.

        Written as a text assertion on purpose. The broken call sat in the
        `--file` branch, which no test reaches without filing real board cards,
        so a behavioural test would not have caught it either.
        """
        with open(SCRIPT) as fh:
            source = fh.read()
        self.assertNotRegex(
            source,
            r"handoff_date\([^)]*\)\s*\.date\(\)",
            "an inline handoff_date(...) call is treating the timestamp as a datetime")
        for var in set(re.findall(r"(\w+)\s*=\s*handoff_date\(", source)):
            # assertFalse, not assertNotIn: assertNotIn prints the container,
            # and the container here is the whole 22 KB script.
            self.assertFalse(
                var + ".date()" in source,
                "%s holds a POSIX timestamp from handoff_date; .date() raises on a "
                "float. Wrap it: datetime.fromtimestamp(%s).date()" % (var, var))


if __name__ == "__main__":
    unittest.main()
