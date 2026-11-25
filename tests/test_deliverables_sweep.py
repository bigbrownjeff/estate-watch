"""Tests for local-tools/deliverables-sweep.py, the routed (outbox) sweep.

Two layers:
  * SweepCounting* / Rehearsal*: the notes_vault modules are replaced by a fake
    vault (the one seam, `_load_vault_modules`), so every branch of the loop is
    driven without a network, a gcloud call, a real outbox or any real vault.
  * RealVault*: the real notes_vault library against scratch vaults under a temp
    directory (a local one and a moved one with its own NV_OUTBOX). The notes
    vault checkout is only read for code; its data directory is never touched.
    Skipped, loudly, when the checkout is absent (set NOTES_VAULT_REPO).

Every test runs against temp directories. failtask is always stubbed.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "local-tools" / "deliverables-sweep.py"
NOTES_VAULT_REPO = Path(os.environ.get("NOTES_VAULT_REPO")
                        or Path.home() / "Projects" / "notes-vault")
_loads = 0

FINAL_KEYS = ["files_seen", "written", "queued", "skipped", "refused", "errors",
              "skipped_optout", "moved", "flush_sent", "flush_refused", "flush_left",
              "import_found", "import_inserted", "import_updated"]


def load_sweep(rehearsal=False):
    """A fresh copy of the script. REHEARSAL is read at import time, so the
    environment is set before the load and restored after it."""
    global _loads
    _loads += 1
    # no .pyc: a stale one (same size, same second) would hide an edit to the script
    with mock.patch.dict(os.environ), mock.patch.object(sys, "dont_write_bytecode", True):
        if rehearsal:
            os.environ["NV_REHEARSAL"] = "1"
        else:
            os.environ.pop("NV_REHEARSAL", None)
        spec = importlib.util.spec_from_file_location("deliverables_sweep_t%d" % _loads, SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(mod)
    return mod


def fake_hash(title, tags, body):
    return hashlib.sha256(json.dumps([title, list(tags or []), body]).encode()).hexdigest()


class FakeVault:
    """Stands in for the tuple `_load_vault_modules` returns."""

    def __init__(self, moved):
        self.moved = moved
        self.known = {}          # note_id -> hash the record last took
        self.raises = {}         # file base name -> exception add_note raises for it
        self.probe = None        # called with the conn handed to add_note
        self.during_import = []  # what importer.import_dir returned inside add_note
        self.pending = {}        # event_id -> item dict (with "_hash"), for the fix
        self.unreadable = set()  # event ids whose read_item raises FramingError

        self.OutboxRefusal = type("OutboxRefusal", (ValueError,), {})
        self.FramingError = type("FramingError", (Exception,), {})
        self.connect = mock.Mock(side_effect=lambda path: sqlite3.connect(":memory:"))
        self.add_note = mock.Mock(side_effect=self._add_note)
        self.import_dir = mock.Mock(return_value={"found": 3, "inserted": 2, "updated": 1})
        self.flush = mock.Mock(return_value=self.flush_stats())
        self.authority = mock.Mock()
        self.importer = SimpleNamespace(import_dir=self.import_dir)

    @staticmethod
    def flush_stats(**kw):
        base = {"sent": 0, "refused": 0, "deferred": 0, "left": 0, "paused": False,
                "busy": False, "error": None}
        base.update(kw)
        return base

    def _add_note(self, conn, **kw):
        if self.probe:
            self.probe(conn)
        if not self.moved:
            # the real add_note reimports the log dir per file; the sweep must have
            # turned that into a no-op for the run
            self.during_import.append(self.importer.import_dir(conn, "x", source_app="deliverable"))
        for needle, exc in self.raises.items():
            if needle in kw["title"]:
                raise exc
        return {"queued": True} if self.moved else {"queued": False, "id": 1}

    def modules(self, vault_repo):
        logger = SimpleNamespace(add_note=self.add_note, _content_hash=fake_hash)
        freeze = SimpleNamespace(
            data_dir_for_db=lambda db: os.path.dirname(os.path.abspath(db)),
            read_state=lambda ddir: {"state": "moved"} if self.moved else {},
            routing_target=lambda state: "record" if state.get("state") == "moved" else None)

        def read_item(d):
            ev = os.path.basename(d)
            if ev in self.unreadable:
                raise self.FramingError("bad item")
            return self.pending[ev]

        outbox = SimpleNamespace(
            OutboxRefusal=self.OutboxRefusal, flush=self.flush,
            outbox_root=lambda: "/nonexistent-outbox",
            _norm_log_dir=lambda log_dir, app: log_dir,
            _base_hash=lambda root, ddir, log_dir, app, sid: self.known.get(sid),
            _list_ids=lambda root, sub: sorted(self.pending) if sub == "pending" else [],
            items=SimpleNamespace(read_item=read_item, FramingError=self.FramingError),
            _item_hash=lambda item, d: item["_hash"])
        return (self.connect, logger, self.importer, freeze, outbox,
                lambda text: ({}, text), ValueError, self.authority)


class SweepCase(unittest.TestCase):
    moved = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.projects = root / "projects"
        self.dir = self.projects / "repoA" / "docs" / "deliverables"
        self.dir.mkdir(parents=True)
        self.vault = root / "vault"
        (self.vault / "data").mkdir(parents=True)
        self.db = self.vault / "data" / "notes.db"
        sqlite3.connect(str(self.db)).close()
        self.log_dir = self.vault / "data" / "wiki" / "deliverable"
        self.mod = load_sweep()
        self.fake = FakeVault(self.moved)
        # failtask is never real: the path does not exist and subprocess.run is a stub
        self.subprocess_run = mock.Mock()
        for patch in (mock.patch.object(self.mod, "_load_vault_modules", self.fake.modules),
                      mock.patch.object(self.mod, "SKIP_LIST_PATH", str(root / "no-skip.json")),
                      mock.patch.object(self.mod, "FAILTASK", str(root / "no-failtask")),
                      mock.patch.object(self.mod.subprocess, "run", self.subprocess_run)):
            patch.start()
            self.addCleanup(patch.stop)

    def files(self, *names):
        for name in names:
            (self.dir / name).write_text("# " + name + "\nbody of " + name + "\n", encoding="utf-8")

    def sweep(self, **kw):
        kw.setdefault("projects_root", str(self.projects))
        kw.setdefault("vault_repo", str(self.vault))
        kw.setdefault("db_path", str(self.db))
        kw.setdefault("log_dir", str(self.log_dir))
        return self.mod.run_sweep(**kw)

    def main(self, *args, mod=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = (mod or self.mod).main(["--projects-root", str(self.projects),
                                           "--vault-repo", str(self.vault)] + list(args))
        return code, out.getvalue(), err.getvalue()

    def note_kwargs(self):
        """The kwargs the sweep gave add_note for each file, keyed by note id."""
        return {c.kwargs["note_id"]: c.kwargs for c in self.fake.add_note.call_args_list}


class WriteAuthorityTest(SweepCase):
    # item 1
    def test_routed_authority_is_required_with_the_tool_name(self):
        self.files("a.md")
        self.sweep()
        self.fake.authority.assert_called_once()
        self.assertEqual(self.fake.authority.call_args.kwargs,
                         {"tool": "deliverables-sweep.py", "db_path": str(self.db), "routed": True})

    def test_dry_run_never_asks_for_authority_and_never_writes(self):
        self.files("a.md", "b.md")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            stats = self.sweep(dry_run=True)
        self.fake.authority.assert_not_called()
        self.fake.add_note.assert_not_called()
        self.fake.connect.assert_not_called()
        self.fake.flush.assert_not_called()
        self.assertEqual(out.getvalue().count("DRY-RUN would log:"), 2)
        self.assertEqual(stats["seen"], 2)


class MovedVaultConnectionTest(SweepCase):
    # item 2
    def test_moved_vault_opens_the_replica_immutable_and_leaves_no_side_files(self):
        c = sqlite3.connect(str(self.db))
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE t (a)")
        c.commit()
        c.close()
        data = self.db.parent
        before = sorted(os.listdir(data))
        os.chmod(self.db, 0o444)
        os.chmod(data, 0o555)
        self.addCleanup(os.chmod, data, 0o755)
        self.addCleanup(os.chmod, self.db, 0o644)
        seen = {}

        def probe(conn):
            seen["read"] = conn.execute("SELECT count(*) FROM t").fetchone()[0]
            try:
                conn.execute("CREATE TABLE x (a)")
                seen["write"] = "allowed"
            except sqlite3.OperationalError as exc:
                seen["write"] = str(exc)

        self.fake.probe = probe
        self.files("a.md")
        stats = self.sweep()
        self.assertEqual(stats["queued"], 1)
        self.fake.connect.assert_not_called()  # connect() is the writable path
        self.assertEqual(seen["read"], 0)
        self.assertIn("readonly", seen["write"])
        self.assertEqual(sorted(os.listdir(data)), before)

    def test_local_vault_uses_the_normal_connection(self):
        self.moved = False
        fake = FakeVault(False)
        with mock.patch.object(self.mod, "_load_vault_modules", fake.modules):
            self.files("a.md")
            self.sweep()
        fake.connect.assert_called_once_with(str(self.db))


class SkipTest(SweepCase):
    # item 3
    def test_note_with_the_known_hash_is_skipped_and_add_note_not_called(self):
        self.files("a.md", "b.md")
        self.sweep()
        kw = self.note_kwargs()
        self.assertEqual(len(kw), 2)
        self.fake.add_note.reset_mock()
        for nid, k in kw.items():
            self.fake.known[nid] = fake_hash(k["title"], k["tags"], k["body"])
        stats = self.sweep()
        self.assertEqual((stats["skipped"], stats["queued"], stats["written"]), (2, 0, 0))
        self.fake.add_note.assert_not_called()

    def test_changed_file_is_queued_again_and_the_unchanged_one_skipped(self):
        self.files("a.md", "b.md")
        self.sweep()
        for nid, k in self.note_kwargs().items():
            self.fake.known[nid] = fake_hash(k["title"], k["tags"], k["body"])
        self.fake.add_note.reset_mock()
        (self.dir / "a.md").write_text("# a edited\n", encoding="utf-8")
        stats = self.sweep()
        self.assertEqual((stats["skipped"], stats["queued"]), (1, 1))
        self.assertEqual(self.fake.add_note.call_count, 1)

    def test_a_hash_for_a_different_note_does_not_skip(self):
        self.files("a.md")
        self.fake.known["deliv-someone-else"] = "x"
        self.assertEqual(self.sweep()["skipped"], 0)


class CountingTest(SweepCase):
    # item 4
    def test_new_note_on_a_moved_vault_is_counted_queued(self):
        self.files("a.md", "b.md")
        stats = self.sweep()
        self.assertEqual((stats["queued"], stats["written"], stats["moved"]), (2, 0, True))

    def test_new_note_on_a_local_vault_is_counted_written(self):
        self.fake.moved = False
        self.files("a.md", "b.md")
        stats = self.sweep()
        self.assertEqual((stats["written"], stats["queued"], stats["moved"]), (2, 0, False))

    def test_the_note_is_logged_with_the_deliverable_shape(self):
        self.files("a.md")
        self.sweep()
        k = list(self.note_kwargs().values())[0]
        self.assertEqual(k["source_app"], "deliverable")
        self.assertIn("deliverable", k["tags"])
        self.assertIn("repoa", k["tags"])
        self.assertEqual(k["log_dir"], str(self.log_dir))
        self.assertTrue(k["note_id"].startswith("deliv-repoa-"))

    def test_optout_marker_files_are_counted_and_not_logged(self):
        (self.dir / "x.md").write_text("---\ntags: [no-vault]\n---\nsecret\n", encoding="utf-8")
        self.fake_optout()
        stats = self.sweep()
        self.assertEqual((stats["skipped_optout"], stats["queued"]), (1, 0))

    def fake_optout(self):
        """The fake frontmatter parser reads one 'no-vault' tag line."""
        def parse(text):
            return ({"tags": ["no-vault"]} if "no-vault" in text else {}), text
        real = self.fake.modules

        def modules(repo):
            t = list(real(repo))
            t[5] = parse
            return tuple(t)
        p = mock.patch.object(self.mod, "_load_vault_modules", modules)
        p.start()
        self.addCleanup(p.stop)


class FailureTest(SweepCase):
    # items 5 and 6
    def test_outbox_refusal_is_counted_printed_and_the_loop_continues(self):
        self.files("a.md", "b.md", "c.md")
        self.fake.raises["b.md"] = self.fake.OutboxRefusal("secret scan")
        code, out, err = self.main("--no-flush")
        self.assertEqual(self.fake.add_note.call_count, 3)  # c.md still tried
        refused_path = str(self.dir / "b.md")
        self.assertIn("REFUSED " + refused_path, err)
        self.assertEqual(self.final(out)["refused"], 1)
        self.assertEqual(self.final(out)["queued"], 2)
        self.assertEqual(self.final(out)["errors"], 0)
        self.assertNotEqual(code, 0)

    def test_other_exception_is_counted_as_an_error_and_the_loop_continues(self):
        self.files("a.md", "b.md", "c.md")
        self.fake.raises["a.md"] = RuntimeError("disk on fire")
        code, out, err = self.main("--no-flush")
        self.assertEqual(self.fake.add_note.call_count, 3)
        self.assertIn("ERROR on " + str(self.dir / "a.md"), err)
        self.assertIn("disk on fire", err)
        f = self.final(out)
        self.assertEqual((f["errors"], f["refused"], f["queued"]), (1, 0, 2))
        self.assertNotEqual(code, 0)

    def test_a_clean_run_exits_zero(self):
        self.files("a.md")
        code, out, err = self.main()
        self.assertEqual(code, 0)
        self.assertNotIn("PROBLEM", err)

    def test_flush_refusals_make_the_run_exit_nonzero_and_count_in_refused(self):
        self.files("a.md")
        self.fake.flush.return_value = FakeVault.flush_stats(sent=0, refused=2)
        code, out, err = self.main()
        self.assertNotEqual(code, 0)
        self.assertEqual(self.final(out)["refused"], 2)
        self.assertEqual(self.final(out)["flush_refused"], 2)

    def test_flush_transport_error_makes_the_run_exit_nonzero(self):
        self.files("a.md")
        self.fake.flush.return_value = FakeVault.flush_stats(error="gcloud timed out")
        code, out, err = self.main()
        self.assertNotEqual(code, 0)
        self.assertIn("flush error: gcloud timed out", err)

    @staticmethod
    def final(stdout):
        return final_line(stdout)


def final_line(stdout):
    line = stdout.strip().splitlines()[-1]
    return {k: (v if k == "moved" else int(v)) for k, v in
            (p.split("=") for p in line.split(": ", 1)[1].split())}


class ImportTest(SweepCase):
    # item 7
    def test_import_dir_is_never_called_on_a_moved_vault(self):
        self.files("a.md")
        self.sweep()
        self.fake.import_dir.assert_not_called()
        self.assertIs(self.fake.importer.import_dir, self.fake.import_dir)

    def test_local_vault_defers_the_import_to_one_pass_at_the_end(self):
        self.fake.moved = False
        self.files("a.md", "b.md")
        stats = self.sweep()
        self.assertEqual([r["found"] for r in self.fake.during_import], [0, 0])  # no-op inside the loop
        self.fake.import_dir.assert_called_once()
        self.assertEqual(self.fake.import_dir.call_args.kwargs, {"source_app": "deliverable"})
        self.assertEqual(stats["import"], {"found": 3, "inserted": 2, "updated": 1})
        self.assertIs(self.fake.importer.import_dir, self.fake.import_dir)  # patch undone

    def test_local_import_patch_is_undone_even_when_a_file_fails(self):
        self.fake.moved = False
        self.files("a.md")
        self.fake.raises["a.md"] = RuntimeError("x")
        with contextlib.redirect_stderr(io.StringIO()):
            self.sweep()
        self.assertIs(self.fake.importer.import_dir, self.fake.import_dir)


class FlushTest(SweepCase):
    # item 8
    def test_flush_runs_exactly_once_when_moved_and_something_was_queued(self):
        self.files("a.md", "b.md", "c.md")
        stats = self.sweep()
        self.fake.flush.assert_called_once_with()
        self.assertEqual(stats["queued"], 3)

    def test_flush_does_not_run_when_nothing_was_queued(self):
        self.files("a.md")
        self.sweep()
        for nid, k in self.note_kwargs().items():
            self.fake.known[nid] = fake_hash(k["title"], k["tags"], k["body"])
        self.fake.flush.reset_mock()
        stats = self.sweep()
        self.assertEqual(stats["queued"], 0)
        self.fake.flush.assert_not_called()

    def test_no_flush_flag_suppresses_it(self):
        self.files("a.md")
        code, out, err = self.main("--no-flush")
        self.fake.flush.assert_not_called()
        self.assertEqual(final_line(out)["queued"], 1)
        self.assertEqual(code, 0)

    def test_main_flushes_by_default(self):
        self.files("a.md")
        self.main()
        self.fake.flush.assert_called_once_with()

    def test_local_vault_never_flushes(self):
        self.fake.moved = False
        self.files("a.md")
        self.sweep()
        self.fake.flush.assert_not_called()


class FinalLineTest(SweepCase):
    # item 9
    def test_final_stdout_line_has_the_documented_keys_in_order(self):
        self.files("a.md", "b.md")
        self.fake.flush.return_value = FakeVault.flush_stats(sent=2, left=1)
        code, out, err = self.main()
        line = out.strip().splitlines()[-1]
        self.assertTrue(line.startswith("deliverables-sweep: "))
        keys = [p.split("=")[0] for p in line.split(": ", 1)[1].split()]
        self.assertEqual(keys, FINAL_KEYS)
        f = final_line(out)
        self.assertEqual((f["files_seen"], f["queued"], f["flush_sent"], f["flush_left"],
                          f["moved"]), (2, 2, 2, 1, "true"))

    def test_final_line_is_last_even_when_warnings_print(self):
        self.files("a.md")
        self.fake.flush.return_value = FakeVault.flush_stats(paused=True, deferred=1)
        code, out, err = self.main()
        self.assertEqual(out.strip().splitlines()[-1].split(" ")[0], "deliverables-sweep:")
        self.assertIn("PAUSED", err)

    def test_local_vault_reports_import_counts(self):
        self.fake.moved = False
        self.files("a.md")
        code, out, err = self.main()
        f = final_line(out)
        self.assertEqual((f["written"], f["moved"], f["import_found"], f["import_inserted"],
                          f["import_updated"]), (1, "false", 3, 2, 1))


class LimitTest(SweepCase):
    # item 10
    def test_limit_stops_after_n_candidate_files(self):
        self.files("a.md", "b.md", "c.md", "d.md", "e.md")
        stats = self.sweep(limit=2)
        self.assertEqual((stats["seen"], stats["queued"]), (2, 2))
        self.assertEqual(self.fake.add_note.call_count, 2)

    def test_limit_flag_on_the_command_line(self):
        self.files("a.md", "b.md", "c.md")
        code, out, err = self.main("--limit", "1", "--no-flush")
        self.assertEqual(final_line(out)["files_seen"], 1)
        self.assertEqual(self.fake.add_note.call_count, 1)

    def test_no_limit_sees_every_file(self):
        self.files("a.md", "b.md", "c.md")
        self.assertEqual(self.sweep()["seen"], 3)


class RehearsalTest(SweepCase):
    """Item 11: under NV_REHEARSAL=1 no failtask on any path. Each path is also
    proven live outside rehearsal, so the assertion is not vacuous."""

    def setUp(self):
        super().setUp()
        self.stub = Path(self.tmp.name) / "failtask"
        self.stub.write_text("#!/bin/sh\n", encoding="utf-8")

    def run_scenario(self, rehearsal, scenario):
        mod = load_sweep(rehearsal=rehearsal)
        self.assertEqual(mod.REHEARSAL, rehearsal)
        fake = FakeVault(True)
        self.files("a.md")
        patches = [mock.patch.object(mod, "_load_vault_modules", fake.modules),
                   mock.patch.object(mod, "SKIP_LIST_PATH", str(Path(self.tmp.name) / "none.json")),
                   mock.patch.object(mod, "FAILTASK", str(self.stub)),
                   mock.patch.object(mod.subprocess, "run")]
        started = [p.start() for p in patches]
        run = started[3]
        self.addCleanup(lambda: [p.stop() for p in patches])
        if scenario == "refused":
            fake.raises["a.md"] = fake.OutboxRefusal("secret")
        elif scenario == "error":
            fake.raises["a.md"] = RuntimeError("boom")
        elif scenario == "flush-error":
            fake.flush.return_value = FakeVault.flush_stats(error="down")
        elif scenario == "flush-refused":
            fake.flush.return_value = FakeVault.flush_stats(refused=1)
        elif scenario == "crash":
            patches.append(mock.patch.object(mod, "run_sweep", side_effect=RuntimeError("crash")))
            patches[-1].start()
        code, _, _ = self.main(mod=mod)
        return code, run

    SCENARIOS = ("refused", "error", "flush-error", "flush-refused", "crash")

    def test_rehearsal_never_invokes_failtask_but_still_fails_the_run(self):
        for scenario in self.SCENARIOS:
            with self.subTest(scenario):
                code, run = self.run_scenario(True, scenario)
                run.assert_not_called()
                self.assertNotEqual(code, 0)

    def test_outside_rehearsal_every_one_of_those_paths_files_a_card(self):
        for scenario in self.SCENARIOS:
            with self.subTest(scenario):
                code, run = self.run_scenario(False, scenario)
                self.assertEqual(run.call_count, 1)
                argv = run.call_args.args[0]
                self.assertEqual(argv[:2], [str(self.stub), "outbound"])
                self.assertNotEqual(code, 0)

    def test_clean_run_files_nothing_in_either_mode(self):
        for rehearsal in (True, False):
            with self.subTest(rehearsal=rehearsal):
                code, run = self.run_scenario(rehearsal, "clean")
                run.assert_not_called()
                self.assertEqual(code, 0)


class PendingQueueTest(SweepCase):
    """Item 13 against the fake: a note already waiting in the outbox is not
    queued again. The real-outbox version is RealVaultMovedTest."""

    def pend(self, event, nid, content_hash, op="replace", app="deliverable", seq=1):
        self.fake.pending[event] = {"source_app": app, "source_id": nid, "op": op,
                                    "_hash": content_hash, "origin": {"host": "h", "seq": seq}}

    def hash_of(self):
        self.files("a.md")
        self.sweep()
        (nid, k), = self.note_kwargs().items()
        self.fake.add_note.reset_mock()
        return nid, fake_hash(k["title"], k["tags"], k["body"])

    def test_pending_item_with_the_same_hash_is_skipped(self):
        nid, h = self.hash_of()
        self.pend("ev1", nid, h)
        stats = self.sweep()
        self.assertEqual((stats["skipped"], stats["queued"]), (1, 0))
        self.fake.add_note.assert_not_called()

    def test_pending_item_with_a_stale_hash_does_not_skip(self):
        nid, h = self.hash_of()
        self.pend("ev1", nid, "stale")
        self.assertEqual(self.sweep()["queued"], 1)

    def test_pending_item_of_another_source_app_does_not_skip(self):
        nid, h = self.hash_of()
        self.pend("ev1", nid, h, app="log")
        self.assertEqual(self.sweep()["queued"], 1)

    def test_a_pending_append_is_not_treated_as_the_note_content(self):
        nid, h = self.hash_of()
        self.pend("ev1", nid, h, op="append")
        self.assertEqual(self.sweep()["queued"], 1)

    def test_the_newest_pending_item_wins(self):
        nid, h = self.hash_of()
        self.pend("ev1", nid, h, seq=1)
        self.pend("ev2", nid, "newer-edit", seq=2)
        self.assertEqual(self.sweep()["queued"], 1)

    def test_an_unreadable_pending_item_does_not_stop_the_sweep(self):
        nid, h = self.hash_of()
        self.pend("ev1", nid, h)
        self.fake.unreadable.add("ev1")
        stats = self.sweep()
        self.assertEqual((stats["queued"], stats["errors"]), (1, 0))


@unittest.skipUnless((NOTES_VAULT_REPO / "notes_vault" / "outbox.py").is_file(),
                     "notes-vault checkout not found; set NOTES_VAULT_REPO")
class RealVaultCase(unittest.TestCase):
    """The real notes_vault library over scratch vaults in a temp directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.mod = load_sweep()
        cwd, path = os.getcwd(), list(sys.path)
        self.addCleanup(os.chdir, cwd)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), path))
        self.orig_load = self.mod._load_vault_modules
        (self.connect, self.logger, self.importer, self.freeze, self.outbox,
         self.parse, self.FrontmatterError, _) = self.orig_load(str(NOTES_VAULT_REPO))
        authority = mock.Mock()
        self.authority = authority

        def load(vault_repo):
            return self.orig_load(vault_repo)[:-1] + (authority,)

        p = mock.patch.object(self.mod, "_load_vault_modules", load)
        p.start()
        self.addCleanup(p.stop)
        for p in (mock.patch.object(self.mod, "SKIP_LIST_PATH", str(root / "none.json")),
                  mock.patch.object(self.mod, "FAILTASK", str(root / "no-failtask")),
                  mock.patch.object(self.mod.subprocess, "run", mock.Mock())):
            p.start()
            self.addCleanup(p.stop)

        self.projects = root / "projects"
        self.dir = self.projects / "repoA" / "docs" / "deliverables"
        self.dir.mkdir(parents=True)
        self.vault = root / "vault"
        self.data = self.vault / "data"
        self.data.mkdir(parents=True)
        self.db = self.data / "notes.db"
        self.connect(str(self.db)).close()
        self.log_dir = self.data / "wiki" / "deliverable"
        self.outbox_root = root / "outbox"
        env = mock.patch.dict(os.environ, {"NV_OUTBOX": str(self.outbox_root)})
        env.start()
        self.addCleanup(env.stop)
        saved = self.outbox._ctx["data_dir"]
        self.addCleanup(self.outbox._ctx.__setitem__, "data_dir", saved)

    def files(self, *names):
        for name in names:
            (self.dir / name).write_text("# " + name + "\nbody of " + name + "\n", encoding="utf-8")

    def sweep(self, **kw):
        kw.setdefault("flush", False)
        return self.mod.run_sweep(projects_root=str(self.projects), vault_repo=str(NOTES_VAULT_REPO),
                                  db_path=str(self.db), log_dir=str(self.log_dir), **kw)

    def pending(self):
        d = self.outbox_root / "pending"
        return sorted(os.listdir(d)) if d.is_dir() else []


class RealVaultLocalTest(RealVaultCase):
    def test_local_vault_writes_then_skips_then_rewrites_only_what_changed(self):
        self.files("a.md", "b.md")
        first = self.sweep()
        self.assertEqual((first["written"], first["queued"], first["moved"]), (2, 0, False))
        self.assertEqual(first["import"]["found"], 2)
        again = self.sweep()
        self.assertEqual((again["written"], again["skipped"]), (0, 2))
        (self.dir / "a.md").write_text("# a edited\n", encoding="utf-8")
        third = self.sweep()
        self.assertEqual((third["written"], third["skipped"]), (1, 1))
        notes = sorted(os.listdir(self.log_dir))
        self.assertEqual(len(notes), 2)


class RealVaultMovedTest(RealVaultCase):
    def setUp(self):
        super().setUp()
        self.freeze.write_state(str(self.data), "moved", "test", "tests",
                                moved_to=self.freeze.ROUTE_TARGETS[0])

    def test_moved_vault_queues_into_the_outbox_and_writes_no_note_file(self):
        self.files("a.md", "b.md")
        stats = self.sweep()
        self.assertEqual((stats["queued"], stats["written"], stats["moved"]), (2, 0, True))
        self.assertEqual(len(self.pending()), 2)
        self.assertFalse(self.log_dir.exists())

    def test_note_queued_but_not_yet_sent_is_not_queued_again(self):
        # item 13: the skip used to know only sent and replica hashes
        self.files("a.md", "b.md")
        self.assertEqual(self.sweep()["queued"], 2)
        second = self.sweep()
        self.assertEqual((second["queued"], second["skipped"]), (0, 2))
        self.assertEqual(len(self.pending()), 2)
        (self.dir / "a.md").write_text("# a edited\n", encoding="utf-8")
        third = self.sweep()
        self.assertEqual((third["queued"], third["skipped"]), (1, 1))
        self.assertEqual(len(self.pending()), 3)  # the edit queues behind the first one

    def test_a_queued_note_sent_by_the_outbox_stays_skipped(self):
        self.files("a.md")
        self.sweep()
        (ev,) = self.pending()
        item = self.outbox.items.read_item(str(self.outbox_root / "pending" / ev))
        d = str(self.outbox_root / "pending" / ev)
        receipt = {"status": "applied", "event_id": ev}
        self.outbox._note_sent(str(self.outbox_root), item, d, receipt)
        os.rename(d, str(self.outbox_root / "sent" / ev))
        again = self.sweep()
        self.assertEqual((again["queued"], again["skipped"]), (0, 1))
        self.assertEqual(self.pending(), [])

    def test_moved_sweep_leaves_the_replica_tree_unchanged(self):
        self.files("a.md")
        before = sorted(os.listdir(self.data))
        self.sweep()
        self.assertEqual(sorted(os.listdir(self.data)), before)  # no WAL, no wiki dir


if __name__ == "__main__":
    unittest.main()
