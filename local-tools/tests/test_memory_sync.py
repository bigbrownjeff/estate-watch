"""Unit tests for local-tools/memory-sync (stdlib only).

Every test runs against DISPOSABLE fixture profile dirs under a tempdir --
ms.PROFILES / ms.VAULT / ms.LOCKDIR / ms.HOME / ms.FAILTASK are monkeypatched
in setUp/tearDown. Never imports or touches a live ~/.claude* path.

Run: python3 -m unittest local-tools/tests/test_memory_sync.py -v
"""

import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "memory-sync")

_loader = SourceFileLoader("memory_sync", SCRIPT)
_spec = importlib.util.spec_from_loader("memory_sync", _loader)
ms = importlib.util.module_from_spec(_spec)
_loader.exec_module(ms)


def run(args):
    """Call ms.main() with sys.argv patched; return (code, captured_stdout)."""
    old_argv = sys.argv
    sys.argv = ["memory-sync"] + args
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = ms.main()
    finally:
        sys.argv = old_argv
    return code, buf.getvalue()


class Base(unittest.TestCase):
    PROFILE_NAMES = ("main", "claudine", "claudette")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memsync-test-")
        self._orig = dict(PROFILES=dict(ms.PROFILES), VAULT=ms.VAULT,
                           LOCKDIR=ms.LOCKDIR, HOME=ms.HOME, FAILTASK=ms.FAILTASK)
        home = os.path.join(self.tmp, "home")
        ms.PROFILES = {n: os.path.join(home, ".claude" if n == "main" else ".claude-" + n)
                        for n in self.PROFILE_NAMES}
        ms.VAULT = os.path.join(self.tmp, "vault")
        ms.LOCKDIR = os.path.join(self.tmp, "lockdir")
        ms.HOME = home
        ms.FAILTASK = os.path.join(self.tmp, "no-such-failtask")
        for d in ms.PROFILES.values():
            os.makedirs(os.path.join(d, ms.REL), exist_ok=True)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(ms, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mem(self, profile, filename, fm_lines, body=""):
        d = ms.memdir(profile)
        os.makedirs(d, exist_ok=True)
        text = "---\n" + "\n".join(fm_lines) + "\n---\n" + body
        path = os.path.join(d, filename)
        with open(path, "w") as fh:
            fh.write(text)
        # Backdate past FRESH_SECONDS: a file just written in the same test
        # would otherwise look "mid-edit" and get DEFERred by --apply.
        old = time.time() - (ms.FRESH_SECONDS + 60)
        os.utime(path, (old, old))
        return text

    def index(self, profile, content):
        d = ms.memdir(profile)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, ms.INDEX), "w") as fh:
            fh.write(content)


# --------------------------------------------------------------- MEM-2 / registry

class TestRegistry(Base):
    def test_registry_check_fails_when_a_claude_profile_dir_is_unregistered(self):
        # A fourth store dir exists on disk (like claudeux did) but PROFILES
        # doesn't know about it.
        ghost = os.path.join(ms.HOME, ".claude-ghost", ms.REL)
        os.makedirs(ghost, exist_ok=True)
        with open(os.path.join(ghost, ms.INDEX), "w") as fh:
            fh.write("# index\n")
        found = ms.unregistered_profile_dirs()
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].endswith(".claude-ghost"))

        code, out = run(["--check"])
        self.assertIn("unregistered profile dir", out)
        self.assertIn(".claude-ghost", out)

    def test_registry_check_clean_when_every_store_dir_is_registered(self):
        self.assertEqual(ms.unregistered_profile_dirs(), [])


class TestSyncPropagation(Base):
    def test_dry_run_plan_lists_expected_add_counts_per_profile(self):
        self.mem("main", "rule-a.md", ["type: feedback"], "Body A\n")
        code, out = run([])
        self.assertEqual(code, 0)
        self.assertIn("ADD", out)
        self.assertIn("rule-a.md", out)
        self.assertIn("claudine", out)
        self.assertIn("claudette", out)
        self.assertIn("totals: 1 add", out)

    def test_synthetic_feedback_memory_propagates_to_all_registered_profiles_with_index_entry(self):
        self.mem("main", "rule-b.md", ["type: feedback"], "Do the thing.\n")
        self.index("main", "# Memory index\n- [Rule B](rule-b.md) - do it\n")
        code, out = run(["--apply"])
        self.assertEqual(code, 0)
        for p in self.PROFILE_NAMES:
            self.assertTrue(os.path.exists(os.path.join(ms.memdir(p), "rule-b.md")), p)
        for p in ("claudine", "claudette"):
            idx = ms.read(ms.index_path(p))
            self.assertIn("rule-b.md", idx)

    def test_project_and_reference_type_memories_stay_local(self):
        self.mem("main", "wip.md", ["type: project"], "current sprint state\n")
        self.mem("main", "conn.md", ["type: reference"], "mcp wiring\n")
        code, out = run(["--apply"])
        self.assertEqual(code, 0)
        for p in ("claudine", "claudette"):
            self.assertFalse(os.path.exists(os.path.join(ms.memdir(p), "wip.md")))
            self.assertFalse(os.path.exists(os.path.join(ms.memdir(p), "conn.md")))
        self.assertIn("local-only: 2", out)

    def test_apply_creates_snapshot_before_first_write(self):
        self.mem("main", "rule-c.md", ["type: feedback"], "Body C\n")
        self.assertFalse(os.path.isdir(ms.snapshots_dir()))
        code, out = run(["--apply"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isdir(ms.snapshots_dir()))
        tars = [f for f in os.listdir(ms.snapshots_dir()) if f.endswith(".tar.gz")]
        self.assertEqual(len(tars), 1)


# --------------------------------------------------------------- MEM-3 / index

class TestIndex(Base):
    def test_plan_index_multilink_copies_only_matching_segment(self):
        self.mem("main", "a.md", ["type: feedback"], "A\n")
        self.mem("main", "b.md", ["type: project"], "B local only\n")  # ineligible sibling
        self.index("main",
                    "# Memory index\n"
                    "- [A title](a.md) - hookA · [B title](b.md) - hookB\n")
        ops = ms.plan_index("a.md", "main", ["claudine"])
        self.assertEqual(len(ops), 1)
        p, mode, name, payload = ops[0]
        self.assertEqual(mode, "append")
        self.assertIn("a.md", payload)
        self.assertNotIn("b.md", payload)

    def test_plan_index_skips_ineligible_sibling_link(self):
        self.mem("main", "a.md", ["type: feedback"], "A\n")
        self.mem("main", "b.md", ["type: project"], "B\n")
        self.index("main",
                    "# Memory index\n"
                    "- [A title](a.md) - hookA · [B title](b.md) - hookB\n")
        code, out = run(["--apply"])
        self.assertEqual(code, 0)
        idx = ms.read(ms.index_path("claudine"))
        self.assertIn("a.md", idx)
        self.assertNotIn("b.md", idx)

    def test_write_index_merges_segment_into_existing_line(self):
        self.index("claudine",
                    "# Memory index\n"
                    "- [A title](a.md) - hookA · [C title](c.md) - hookC\n")
        ok = ms.write_index("claudine", "a.md", "[A title v2](a.md) - hookA2", "merge")
        self.assertTrue(ok)
        idx = ms.read(ms.index_path("claudine"))
        self.assertIn("hookA2", idx)
        self.assertIn("c.md", idx)  # sibling untouched
        self.assertNotIn("hookA \xb7", idx)

    def test_check_exits_nonzero_on_dangling_link(self):
        self.index("main", "# Memory index\n- [Ghost](ghost.md) - nothing here\n")
        code, out = run(["--check"])
        self.assertEqual(code, 1)
        self.assertIn("dangling index link", out)
        self.assertIn("ghost.md", out)

    def test_check_exits_nonzero_on_unindexed_eligible(self):
        self.mem("main", "orphan.md", ["type: feedback"], "no index entry\n")
        self.mem("claudine", "orphan.md", ["type: feedback"], "no index entry\n")
        self.mem("claudette", "orphan.md", ["type: feedback"], "no index entry\n")
        code, out = run(["--check"])
        self.assertEqual(code, 1)
        self.assertIn("unindexed eligible memory", out)
        self.assertIn("orphan.md", out)

    def test_check_warns_over_byte_budget_with_line_list(self):
        big = "\n".join("- [Item %d](item%d.md) - hook" % (i, i) for i in range(1200))
        self.index("main", "# Memory index\n" + big + "\n")
        code, out = run(["--check"])
        self.assertIn("MEMORY.md is", out)
        self.assertIn("budget", out)
        self.assertIn("entries past boundary at line(s)", out)

    def test_check_green_means_target_can_retrieve_named_rule(self):
        self.mem("main", "rule-d.md", ["type: feedback"], "Rule D body\n")
        self.index("main", "# Memory index\n- [Rule D](rule-d.md) - hook\n")
        code, _ = run(["--apply"])
        self.assertEqual(code, 0)
        code, out = run(["--check"])
        self.assertEqual(code, 0, out)
        for p in self.PROFILE_NAMES:
            self.assertTrue(os.path.exists(os.path.join(ms.memdir(p), "rule-d.md")))
            self.assertIn("rule-d.md", ms.read(ms.index_path(p)))

    def test_per_profile_dangling_link_report_lists_profile_and_target(self):
        # CD-R4-1: claudette-only dangling link must show up, not just main's.
        self.index("main", "# Memory index\n")
        self.index("claudette", "# Memory index\n- [Gone](gone.md) - nothing\n")
        code, out = run(["--check"])
        self.assertEqual(code, 1)
        self.assertIn("claudette", out)
        self.assertIn("gone.md", out)
        self.assertNotIn("main line", out)

    def test_duplicate_index_link_in_one_profile_notes_but_does_not_change_exit(self):
        # W6-V1: a target linked twice in the same profile's index is a
        # warn-only NOTE (like before this scan moved into index_health()),
        # never a --check failure on its own.
        self.mem("main", "rule-e.md", ["type: feedback"], "Rule E body\n")
        self.index("main", "# Memory index\n"
                            "- [Rule E](rule-e.md) - hook\n"
                            "- [Rule E again](rule-e.md) - hook\n")
        code, out = run(["--apply"])
        self.assertEqual(code, 0)
        code, out = run(["--check"])
        self.assertEqual(code, 0, out)
        self.assertIn("duplicate index line", out)
        self.assertIn("main: rule-e.md x2", out)


# --------------------------------------------------------------- MEM-4 / decide, superset, lock

class TestDecide(Base):
    def test_superset_rejects_reordered_lines(self):
        a = "ALLOW x\nDENY y\n"
        b = "DENY y\nALLOW x\n"
        self.assertFalse(ms.superset(a, b))
        self.assertFalse(ms.superset(b, a))

    def test_superset_rejects_negation_append(self):
        a = "Do not publish\nPublish automatically\n"
        b = "Do not publish\n"
        self.assertFalse(ms.superset(a, b))

    def test_decide_conflicts_on_pure_reorder(self):
        per = {
            "main": {"text": "ALLOW x\nDENY y\n", "sha": ms.sha("ALLOW x\nDENY y\n"),
                     "mtime": 0, "modified": "", "eligible": True, "reason": "type: feedback"},
            "claudine": {"text": "DENY y\nALLOW x\n", "sha": ms.sha("DENY y\nALLOW x\n"),
                         "mtime": 0, "modified": "", "eligible": True, "reason": "type: feedback"},
        }
        action, winner, detail = ms.decide("x.md", per)
        self.assertEqual(action, "conflict")

    def test_decide_conflicts_on_contradictory_append(self):
        base = "Do not publish\n"
        appended = "Do not publish\nPublish automatically\n"
        per = {
            "main": {"text": base, "sha": ms.sha(base), "mtime": 0, "modified": "",
                     "eligible": True, "reason": "type: feedback"},
            "claudine": {"text": appended, "sha": ms.sha(appended), "mtime": 0, "modified": "",
                         "eligible": True, "reason": "type: feedback"},
        }
        action, winner, detail = ms.decide("x.md", per)
        self.assertEqual(action, "conflict")

    def test_decide_promotes_pure_append_unchanged(self):
        base = "Rule A applies.\n"
        appended = "Rule A applies.\nAlso rule B applies.\n"
        per = {
            "main": {"text": base, "sha": ms.sha(base), "mtime": 0, "modified": "",
                     "eligible": True, "reason": "type: feedback"},
            "claudine": {"text": appended, "sha": ms.sha(appended), "mtime": 0, "modified": "",
                         "eligible": True, "reason": "type: feedback"},
        }
        action, winner, detail = ms.decide("x.md", per)
        self.assertEqual(action, "promote")
        self.assertEqual(winner, "claudine")

    def test_decide_flags_divergent_ineligible_copy(self):
        elig_text = "Doctrine body.\n"
        other_text = "Different local note.\n"
        per = {
            "main": {"text": elig_text, "sha": ms.sha(elig_text), "mtime": 0, "modified": "",
                     "eligible": True, "reason": "type: feedback"},
            "claudine": {"text": other_text, "sha": ms.sha(other_text), "mtime": 0, "modified": "",
                         "eligible": False, "reason": "type: reference"},
        }
        action, winner, detail = ms.decide("x.md", per)
        self.assertEqual(action, "divergent-ineligible")
        self.assertIn("claudine", detail)


class TestLock(Base):
    def test_lock_stale_grants_exactly_one_owner(self):
        os.makedirs(ms.LOCKDIR)
        old = time.time() - (ms.LOCK_STALE_SECONDS + 60)
        os.utime(ms.LOCKDIR, (old, old))
        self.assertTrue(ms.lock())
        with open(ms._owner_path()) as fh:
            token = fh.read()
        self.assertIn("pid=%d" % os.getpid(), token)
        # A fresh lock (just acquired) must NOT look stale to a second caller.
        self.assertFalse(ms.lock())

    def test_unlock_noop_when_owner_token_foreign(self):
        os.makedirs(ms.LOCKDIR)
        with open(ms._owner_path(), "w") as fh:
            fh.write("pid=999999 host=elsewhere started_at=x\n")
        ms.unlock()
        self.assertTrue(os.path.isdir(ms.LOCKDIR))

    def test_unlock_removes_when_owner_matches(self):
        ms.lock()
        ms.unlock()
        self.assertFalse(os.path.isdir(ms.LOCKDIR))


class TestIO(Base):
    def test_atomic_write_raises_on_concurrent_change(self):
        p = os.path.join(ms.memdir("main"), "x.md")
        ms.atomic_write(p, "v1\n")
        with self.assertRaises(RuntimeError):
            ms.atomic_write(p, "v3\n", expect="v2\n")

    def test_retire_removes_file_and_index_line_in_every_profile(self):
        for prof in self.PROFILE_NAMES:
            self.mem(prof, "old.md", ["type: feedback"], "stale\n")
            self.index(prof, "# Memory index\n- [Old](old.md) - stale\n")
        code = ms.retire("old.md", True)
        self.assertEqual(code, 0)
        for prof in self.PROFILE_NAMES:
            self.assertFalse(os.path.exists(os.path.join(ms.memdir(prof), "old.md")))
            self.assertNotIn("old.md", ms.read(ms.index_path(prof)))

    def test_restore_from_snapshot_round_trip(self):
        self.mem("main", "keep.md", ["type: feedback"], "v1\n")
        snap = ms.snapshot("test")
        p = os.path.join(ms.memdir("main"), "keep.md")
        os.unlink(p)
        self.assertFalse(os.path.exists(p))
        import tarfile
        restore_dir = os.path.join(self.tmp, "restore")
        with tarfile.open(snap) as tar:
            tar.extractall(restore_dir)
        restored = os.path.join(restore_dir, "main", "keep.md")
        self.assertTrue(os.path.exists(restored))
        restored_text = ms.read(restored)
        self.assertIn("v1\n", restored_text)
        self.assertIn("type: feedback", restored_text)
        # Simulate the documented restore: copy the extracted store back over
        # the live-equivalent path.
        shutil.copy(restored, p)
        self.assertEqual(ms.read(p), restored_text)


if __name__ == "__main__":
    unittest.main()
