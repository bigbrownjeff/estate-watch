#!/usr/bin/env python3
"""Tests for failtask's age-escalation feature (added 2026-09-19).

Two harnesses, used for different halves of the feature:

  - REAL_BASH: shells out to the actual `local-tools/failtask --stuck-report`
    with HOME pointed at a temp dir holding a synthetic failures.jsonl. This
    exercises the real /usr/bin/python3 3.9 interpreter end to end, which the
    exec-harness below never touches, and needs no gh/network (--stuck-report
    returns before find_gh() is ever called).

  - EMBEDDED: extracts the python heredoc from failtask and exec()s it with
    `subprocess` stubbed, same pattern as test_failtask_issue_path.py. Used
    for the live escalation path (should_escalate/maybe_escalate/
    record_outcome/board_create), which needs a controllable `now` and a
    stubbed gh — things --stuck-report alone can't give us.

Run: python3 -m pytest local-tools/tests/test_failtask_age_escalation.py -v
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
# Both layouts (repo: local-tools/failtask + local-tools/tests/; installed:
# ~/.claude/bin/failtask + ~/.claude/bin/tests/) put the tool one directory
# above this test file; FAILTASK_PATH overrides for anything else.
FAILTASK_PATH = os.environ.get("FAILTASK_PATH") or os.path.normpath(
    os.path.join(HERE, "..", "failtask"))


# --------------------------------------------------------------- REAL_BASH harness

def iso_days_ago(n, hour=12):
    """ISO-8601 timestamp `n` local days ago, in the same shape failtask
    writes (isoformat(timespec="seconds") on an aware datetime)."""
    dt = (datetime.now(timezone.utc).astimezone() - timedelta(days=n))
    dt = dt.replace(hour=hour, minute=0, second=0, microsecond=0)
    return dt.isoformat(timespec="seconds")


def make_row(key, days_ago, project="proj", title="Title", label="FAILURE", detail=""):
    return json.dumps({
        "ts": iso_days_ago(days_ago), "project": project, "title": title,
        "detail": detail, "key": key, "severity": "warn", "label": label,
        "host": "testhost",
    }, separators=(",", ":"))


def run_stuck_report(lines, env_extra=None):
    """Runs the real script's --stuck-report against a synthetic log under a
    temp HOME. Reads no real board, writes nothing outside the temp dir."""
    tmp = tempfile.mkdtemp(prefix="failtask-age-test-")
    try:
        faildir = os.path.join(tmp, ".claude", "failures")
        os.makedirs(faildir, exist_ok=True)
        with open(os.path.join(faildir, "failures.jsonl"), "w") as f:
            for line in lines:
                f.write(line + "\n")
        env = dict(os.environ)
        env["HOME"] = tmp
        env.pop("FAILTASK_STUCK_DAYS", None)
        env.pop("FAILTASK_STUCK_WINDOW_DAYS", None)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(["bash", FAILTASK_PATH, "--stuck-report"],
                               capture_output=True, text=True, timeout=30, env=env)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class StuckReportTests(unittest.TestCase):
    """1, 2, 3, 6, 9(a): the report mode, driven through the real interpreter."""

    def test_1_density_threshold_reports_5_not_4(self):
        lines = [make_row("dense:five", d, title="Five day flake") for d in (0, 3, 6, 9, 13)]
        lines += [make_row("dense:four", d, title="Four day flake") for d in (0, 3, 6, 9)]
        r = run_stuck_report(lines)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("key=dense:five", r.stdout)
        self.assertIn("days=5/14", r.stdout)
        self.assertNotIn("key=dense:four", r.stdout)
        self.assertEqual(r.stdout.strip().splitlines()[-1],
                          "1 key(s) would escalate now (density >= 5 of last 14 days, active in last 48h)")

    def test_2_flapping_key_counts_distinct_days_not_streak(self):
        # Every other day for 14 days: 7 distinct days, longest CONSECUTIVE
        # streak is 1. A streak-based check would wrongly miss this.
        lines = [make_row("flap:key", d, title="Flapper") for d in (0, 2, 4, 6, 8, 10, 12)]
        r = run_stuck_report(lines)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("key=flap:key", r.stdout)
        self.assertIn("days=7/14", r.stdout)

    def test_3_rows_outside_window_are_excluded(self):
        # 3 distinct days inside the 14-day window (not enough alone) plus 3
        # more well outside it. If the old rows counted, density would be 6.
        lines = [make_row("old:notenough", d) for d in (0, 3, 6)]
        lines += [make_row("old:notenough", d) for d in (20, 25, 30)]
        r = run_stuck_report(lines)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("key=old:notenough", r.stdout)
        self.assertEqual(r.stdout.strip().splitlines()[-1],
                          "0 key(s) would escalate now (density >= 5 of last 14 days, active in last 48h)")

    def test_6_stuck_days_zero_disables_report(self):
        lines = [make_row("any:key", d) for d in range(7)]
        r = run_stuck_report(lines, env_extra={"FAILTASK_STUCK_DAYS": "0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(),
                          "failtask --stuck-report: escalation disabled (FAILTASK_STUCK_DAYS=0)")

    def test_9a_malformed_and_missing_ts_rows_do_not_break_the_report(self):
        lines = [make_row("dense:five", d) for d in (0, 3, 6, 9, 13)]
        lines.insert(2, "{not valid json at all")
        lines.append(json.dumps({"project": "p", "title": "no ts here",
                                  "key": "dense:five", "label": "FAILURE"}))
        r = run_stuck_report(lines)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("key=dense:five", r.stdout)
        self.assertIn("days=5/14", r.stdout)


# --------------------------------------------------------------- EMBEDDED harness

def extract_embedded_python():
    src = open(FAILTASK_PATH).read()
    m = re.search(r"<<'PYEOF'\n(.*)\nPYEOF\n", src, re.S)
    assert m, "could not find the python heredoc in failtask"
    body = m.group(1)
    marker = "\ntry:\n    main()"
    idx = body.index(marker)
    return body[:idx]  # drop the auto-invoked main()/sys.exit(0) tail


EMBEDDED_SRC = extract_embedded_python()


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def load_module(dispatch=None):
    """Exec the embedded source with `subprocess` stubbed. Returns
    (namespace, calls), calls being every command list passed to
    subprocess.run, in order. No real process ever runs."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if dispatch is not None:
            r = dispatch(cmd)
            if r is not None:
                return r
        joined = " ".join(cmd)
        if "issue create" in joined:
            return FakeResult(0, stdout="https://github.com/bigbrownjeff/board/issues/999\n")
        if "item-add" in joined:
            return FakeResult(0, stdout=json.dumps({"id": "PVTI_stucknew"}))
        if "item-create" in joined:
            return FakeResult(0, stdout=json.dumps({"id": "PVTI_stuckdraft"}))
        return FakeResult(0, stdout="")

    fake_subprocess = types.ModuleType("subprocess")
    fake_subprocess.run = fake_run
    real_subprocess = sys.modules.get("subprocess")
    sys.modules["subprocess"] = fake_subprocess
    ns = {}
    try:
        exec(compile(EMBEDDED_SRC, "failtask_age_embedded", "exec"), ns)
    finally:
        if real_subprocess is not None:
            sys.modules["subprocess"] = real_subprocess
        else:
            del sys.modules["subprocess"]
    return ns, calls


def dt(y, m, d, hh=12):
    return datetime(y, m, d, hh, 0, 0, tzinfo=timezone.utc)


def mkrow(key, when, project="infra", title="T", label="FAILURE", detail=""):
    return {"ts": when.isoformat(timespec="seconds"), "project": project, "title": title,
            "detail": detail, "key": key, "severity": "warn", "label": label, "host": "h"}


def write_failure_rows(path, rows):
    with open(path, "w") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")


class EscalationDecisionTests(unittest.TestCase):
    """4, 5: should_escalate()'s own gates, direct and precise."""

    def test_4_stuck_prefixed_keys_and_non_failure_labels_never_escalate(self):
        ns, _ = load_module()
        self.assertFalse(ns["should_escalate"]({"key": "stuck:already", "label": "FAILURE"}, "dupe", False))
        self.assertFalse(ns["should_escalate"]({"key": "plain", "label": "OPEN THREAD"}, "dupe", False))
        self.assertFalse(ns["should_escalate"]({"key": "plain", "label": "STANDING"}, "reopened", False))
        self.assertTrue(ns["should_escalate"]({"key": "plain", "label": "FAILURE"}, "dupe", False))

    def test_5_no_escalate_flag_suppresses(self):
        ns, _ = load_module()
        rec = {"key": "plain", "label": "FAILURE"}
        self.assertFalse(ns["should_escalate"](rec, "dupe", True))
        self.assertFalse(ns["should_escalate"](rec, "reopened", True))
        self.assertTrue(ns["should_escalate"](rec, "dupe", False))
        self.assertFalse(ns["should_escalate"](rec, "created", False))


class StuckKeyNamingTests(unittest.TestCase):
    """7: the escalation key is exactly stuck:<key>, unchanged by the date."""

    def test_7_stuck_key_has_no_date_and_is_stable_across_days(self):
        tmp = tempfile.mkdtemp(prefix="failtask-age-test-")
        try:
            jsonl_path = os.path.join(tmp, "failures.jsonl")
            write_failure_rows(jsonl_path, [mkrow("densekey", dt(2026, 9, d)) for d in (1, 2, 3, 4, 5)])

            def marker_for(now):
                ns, calls = load_module()
                ns["JSONL"] = jsonl_path
                ns["PENDING"] = os.path.join(tmp, "pending.jsonl")
                rec = mkrow("densekey", now, title="Dense flake")
                ns["maybe_escalate"]("FAKE_GH", rec, "dupe", [], None, False, now=now)
                for c in calls:
                    if "issue" in c and "create" in c and "--body" in c:
                        body = c[c.index("--body") + 1]
                        m = re.search(r"failkey: (stuck:\S+)", body)
                        if m:
                            return m.group(1)
                return None

            key1 = marker_for(dt(2026, 9, 10))
            key2 = marker_for(dt(2026, 9, 11))
            self.assertEqual(key1, "stuck:densekey")
            self.assertEqual(key2, "stuck:densekey")
            self.assertEqual(key1, key2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class BoardPathEscalationTests(unittest.TestCase):
    """8: the escalation card is filed, deduped and survives an internal raise."""

    def test_8_dupe_outcome_escalates_dedupes_and_survives_a_raise(self):
        tmp = tempfile.mkdtemp(prefix="failtask-age-test-")
        try:
            jsonl_path = os.path.join(tmp, "failures.jsonl")
            write_failure_rows(jsonl_path, [mkrow("boardkey", dt(2026, 9, d)) for d in (1, 2, 3, 4, 5)])
            now = dt(2026, 9, 10)

            with self.subTest("creates a STANDING escalation card"):
                ns, calls = load_module()
                ns["JSONL"] = jsonl_path
                ns["PENDING"] = os.path.join(tmp, "pending1.jsonl")
                rec = mkrow("boardkey", now, title="Board flake")
                ns["record_outcome"]("FAKE_GH", rec, "dupe", "PVTI_orig", [], None, False, now=now)
                create_calls = [c for c in calls if "issue" in c and "create" in c]
                self.assertEqual(len(create_calls), 1, str(calls))
                title = create_calls[0][create_calls[0].index("--title") + 1]
                body = create_calls[0][create_calls[0].index("--body") + 1]
                self.assertIn("STANDING", title)
                self.assertIn("failkey: stuck:boardkey", body)

            with self.subTest("an existing escalation card makes no gh write"):
                ns, calls = load_module()
                ns["JSONL"] = jsonl_path
                ns["PENDING"] = os.path.join(tmp, "pending2.jsonl")
                rec = mkrow("boardkey", now, title="Board flake")
                items = [{"id": "PVTI_stuck_existing", "status": "Todo",
                          "content": {"type": "Issue", "id": "I_x",
                                      "body": "ctx\nfailkey: stuck:boardkey"}}]
                ns["record_outcome"]("FAKE_GH", rec, "dupe", "PVTI_orig", items, None, False, now=now)
                self.assertEqual(calls, [], str(calls))

            with self.subTest("an escalation error still leaves the ordinary row and never raises"):
                ns, calls = load_module()
                jsonl_path2 = os.path.join(tmp, "failures2.jsonl")
                ns["JSONL"] = jsonl_path2
                ns["PENDING"] = os.path.join(tmp, "pending3.jsonl")
                ns["key_stuck_stats"] = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
                rec = mkrow("boardkey", now, title="Board flake")
                ns["record_outcome"]("FAKE_GH", rec, "dupe", "PVTI_orig", [], None, False, now=now)
                with open(jsonl_path2) as f:
                    written = [json.loads(l) for l in f if l.strip()]
                self.assertEqual(len(written), 1, written)
                self.assertEqual(written[0]["key"], "boardkey")
                self.assertTrue(written[0].get("dedupe_hit") is True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class MalformedRowsInProcessTests(unittest.TestCase):
    """9(b): a malformed line and a no-ts row don't break the live call either."""

    def test_9b_read_failure_rows_skips_bad_lines_key_stuck_stats_survives_no_ts(self):
        ns, _ = load_module()
        tmp = tempfile.mkdtemp(prefix="failtask-age-test-")
        try:
            jsonl_path = os.path.join(tmp, "failures.jsonl")
            with open(jsonl_path, "w") as f:
                f.write(json.dumps(mkrow("k", dt(2026, 9, 1))) + "\n")
                f.write("not json\n")
                f.write("\n")
                f.write(json.dumps({"project": "p", "title": "no ts", "key": "k",
                                     "label": "FAILURE"}) + "\n")
                f.write(json.dumps(mkrow("k", dt(2026, 9, 2))) + "\n")
            rows = list(ns["read_failure_rows"](jsonl_path))
            self.assertEqual(len(rows), 3, rows)  # the malformed line and blank line are skipped

            days, rows_in_window, first_seen = ns["key_stuck_stats"](
                jsonl_path, "k", 14, now=dt(2026, 9, 10))
            self.assertEqual(len(days), 2, days)  # the no-ts row counts toward neither
            self.assertEqual(rows_in_window, 2)
            self.assertEqual(first_seen.isoformat(), "2026-09-01")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
