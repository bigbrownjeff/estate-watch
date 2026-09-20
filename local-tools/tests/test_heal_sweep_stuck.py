#!/usr/bin/env python3
"""Offline tests for heal-sweep's stuck class (stuck:<original key>, the
STANDING escalation cards failtask files — memory
standing-failures-need-age-escalation).

heal-sweep.py's filename has a hyphen, so it isn't importable by name; this
harness loads it by path via importlib, the same pattern
test_heal_sweep_burn.py uses (so it tests the repo copy). Each test loads a
fresh module object so the per-sweep failures.jsonl cache never leaks
between tests, and points FAILURES_LOG at a tempdir file so no test ever
reads the real ~/.claude/failures/failures.jsonl.

Run: python3 -m pytest local-tools/tests/test_heal_sweep_stuck.py -q
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
HEAL_SWEEP_PATH = os.path.join(os.path.dirname(HERE), "heal-sweep.py")


def load_heal_sweep():
    spec = importlib.util.spec_from_file_location("heal_sweep_stuck_test", HEAL_SWEEP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc).astimezone()


def ts_days_ago(n, hour=12):
    dt = (NOW - timedelta(days=n)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return dt.isoformat(timespec="seconds")


def filed_ts_days_ago(n):
    dt = NOW - timedelta(days=n)
    return dt.timestamp()


def row(key, days_ago, project="infra", title="Job failing", label="FAILURE"):
    return {"ts": ts_days_ago(days_ago), "project": project, "title": title,
            "key": key, "label": label}


def write_jsonl(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def stuck_card(key, filed_days_ago):
    return {"failkey": "stuck:%s" % key, "filed_ts": filed_ts_days_ago(filed_days_ago)}


class HealSweepStuckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mod = load_heal_sweep()
        self.mod.FAILURES_LOG = os.path.join(self.tmp.name, "failures.jsonl")

    # -------------------------------------------------------------- test 1
    def test_quiet_for_8_days_heals(self):
        # Density stays high (>=5 of 14) so only the quiet threshold decides.
        rows = [row("k1", d) for d in (8, 9, 10, 11, 12)]
        rows += [row("k1", d) for d in range(13, 30)]  # log spans 30 days
        write_jsonl(self.mod.FAILURES_LOG, rows)
        evidence, reason = self.mod.check_stuck(stuck_card("k1", 20), now=NOW)
        self.assertIsNotNone(evidence, reason)
        self.assertIsNone(reason)
        self.assertIn("last row at", evidence)
        self.assertIn("red day", evidence)
        self.assertIn("threshold", evidence)
        self.assertIn("failures.jsonl", evidence)

    def test_quiet_for_6_days_does_not_heal(self):
        rows = [row("k1", d) for d in (6, 9, 10, 11, 12)]
        rows += [row("k1", d) for d in range(13, 30)]
        write_jsonl(self.mod.FAILURES_LOG, rows)
        evidence, reason = self.mod.check_stuck(stuck_card("k1", 20), now=NOW)
        self.assertIsNone(evidence, str((evidence, reason)))
        self.assertIn("still red", reason)

    def test_mutation_quiet_threshold_off_by_one(self):
        """Proof the quiet test actually catches a regression: widening the
        quiet comparison by one day would wrongly heal the 6-day case."""
        rows = [row("k1", d) for d in (6, 9, 10, 11, 12)]
        rows += [row("k1", d) for d in range(13, 30)]
        write_jsonl(self.mod.FAILURES_LOG, rows)
        import inspect
        src = inspect.getsource(self.mod.check_stuck)
        mutated = src.replace(
            "quiet_start = today - timedelta(days=quiet_days - 1)",
            "quiet_start = today - timedelta(days=quiet_days - 2)", 1)
        self.assertNotEqual(src, mutated, "mutation target string not found")
        ns = dict(self.mod.__dict__)
        exec(compile(mutated, "<mutated check_stuck>", "exec"), ns)
        evidence, reason = ns["check_stuck"](stuck_card("k1", 20), now=NOW)
        self.assertIsNotNone(evidence, "mutation was not caught: still refused to heal")

    # -------------------------------------------------------------- test 2
    def test_density_4_of_14_with_row_yesterday_heals(self):
        rows = [row("k2", d) for d in (1, 4, 8, 13)]  # 4 distinct days, most recent yesterday
        rows += [row("other", d) for d in range(14, 30)]  # pads the log span only
        write_jsonl(self.mod.FAILURES_LOG, rows)
        evidence, reason = self.mod.check_stuck(stuck_card("k2", 20), now=NOW)
        self.assertIsNotNone(evidence, reason)
        self.assertIn("4 red day", evidence)

    def test_density_5_of_14_does_not_heal(self):
        rows = [row("k2", d) for d in (1, 4, 6, 8, 13)]  # 5 distinct days
        rows += [row("other", d) for d in range(14, 30)]
        write_jsonl(self.mod.FAILURES_LOG, rows)
        evidence, reason = self.mod.check_stuck(stuck_card("k2", 20), now=NOW)
        self.assertIsNone(evidence, str((evidence, reason)))

    def test_mutation_density_threshold_off_by_one(self):
        """Proof: an off-by-one (< -> <=) on the density comparison wrongly
        heals the 5-of-14 case."""
        rows = [row("k2", d) for d in (1, 4, 6, 8, 13)]
        rows += [row("other", d) for d in range(14, 30)]
        write_jsonl(self.mod.FAILURES_LOG, rows)
        import inspect
        src = inspect.getsource(self.mod.check_stuck)
        mutated = src.replace(
            "density_ok = len(density_days) < threshold",
            "density_ok = len(density_days) <= threshold", 1)
        self.assertNotEqual(src, mutated, "mutation target string not found")
        ns = dict(self.mod.__dict__)
        exec(compile(mutated, "<mutated check_stuck>", "exec"), ns)
        evidence, reason = ns["check_stuck"](stuck_card("k2", 20), now=NOW)
        self.assertIsNotNone(evidence, "mutation was not caught: still refused to heal")

    # -------------------------------------------------------------- test 3
    def test_flapping_key_never_heals(self):
        rows = [row("flap", d) for d in range(0, 30, 2)]  # red every other day
        write_jsonl(self.mod.FAILURES_LOG, rows)
        evidence, reason = self.mod.check_stuck(stuck_card("flap", 25), now=NOW)
        self.assertIsNone(evidence, str((evidence, reason)))
        self.assertIn("still red", reason)

    # -------------------------------------------------------------- test 4
    def test_missing_log_no_heal(self):
        # FAILURES_LOG points at a tmpdir file that was never written.
        evidence, reason = self.mod.check_stuck(stuck_card("k4", 20), now=NOW)
        self.assertIsNone(evidence)
        self.assertIn(self.mod.FAILURES_LOG, reason)
        self.assertIn("not found", reason)

    def test_empty_log_no_heal(self):
        open(self.mod.FAILURES_LOG, "w").close()
        evidence, reason = self.mod.check_stuck(stuck_card("k4", 20), now=NOW)
        self.assertIsNone(evidence)
        self.assertIn(self.mod.FAILURES_LOG, reason)

    def test_log_too_short_to_trust_no_heal(self):
        # Oldest row only 2 days old, as after a truncation: the window
        # being judged (14 days) can't be vouched for even though the key
        # itself looks quiet within those 2 days.
        rows = [row("k4", 2)]
        write_jsonl(self.mod.FAILURES_LOG, rows)
        evidence, reason = self.mod.check_stuck(stuck_card("k4", 20), now=NOW)
        self.assertIsNone(evidence, str((evidence, reason)))
        self.assertIn(self.mod.FAILURES_LOG, reason)
        self.assertIn("too short", reason)

    def test_mutation_removing_span_guard_wrongly_heals(self):
        """Proof: removing the span guard lets the truncated-log case heal."""
        rows = [row("k4", 2)]
        write_jsonl(self.mod.FAILURES_LOG, rows)
        import inspect
        src = inspect.getsource(self.mod.check_stuck)
        marker = "    if flog[\"oldest_dt\"].date() > span_start:\n"
        self.assertIn(marker, src, "span guard line not found to mutate")
        end_marker = "                          span_start.isoformat()))\n"
        self.assertIn(end_marker, src, "span guard end not found to mutate")
        start_idx = src.index(marker)
        end_idx = src.index(end_marker) + len(end_marker)
        mutated = src[:start_idx] + src[end_idx:]
        self.assertNotEqual(src, mutated)
        ns = dict(self.mod.__dict__)
        exec(compile(mutated, "<mutated check_stuck>", "exec"), ns)
        evidence, reason = ns["check_stuck"](stuck_card("k4", 20), now=NOW)
        self.assertIsNotNone(evidence, "mutation was not caught: span-guardless code still refused to heal")

    # -------------------------------------------------------------- test 5
    def test_bookkeeping_malformed_and_no_ts_rows_are_skipped(self):
        good_rows = [row("k5", d) for d in (8, 9, 10, 11, 12)]
        good_rows += [row("k5", d) for d in range(13, 30)]
        lines = [json.dumps(r) for r in good_rows]
        lines.append(json.dumps({"ts": ts_days_ago(1), "key": "stuck:k5",
                                  "project": "infra", "title": "x", "label": "STANDING"}))
        lines.append("{not valid json")
        lines.append(json.dumps({"key": "k5", "project": "infra", "title": "no ts"}))
        with open(self.mod.FAILURES_LOG, "w") as f:
            f.write("\n".join(lines) + "\n")
        evidence, reason = self.mod.check_stuck(stuck_card("k5", 20), now=NOW)
        # If the stuck: bookkeeping row (dated yesterday) wrongly counted as
        # the original key firing, this would refuse to heal.
        self.assertIsNotNone(evidence, reason)

    # -------------------------------------------------------------- test 6
    def test_class_of_buckets_stuck(self):
        self.assertEqual(self.mod.class_of("stuck:anything"), "stuck")
        self.assertEqual(self.mod.class_of("stuck:launchd:com.jeffpinto.x"), "stuck")
        self.assertEqual(self.mod.class_of("launchd:com.jeffpinto.x"), "launchd")
        self.assertEqual(self.mod.class_of("burn:agent:a1"), "burn")

    def test_classify_dispatches_stuck(self):
        rows = [row("k6", d) for d in (8, 9, 10, 11, 12)]
        rows += [row("k6", d) for d in range(13, 30)]
        write_jsonl(self.mod.FAILURES_LOG, rows)
        card = stuck_card("k6", 20)
        card["body"] = "failkey: stuck:k6\nfiled: x\n"
        # classify() has no now= hook (it's the production path); pin real
        # time is unnecessary here since we only check dispatch, not dates —
        # use check_stuck directly with now= for the dated assertion above,
        # and confirm classify() routes to it for the same failkey shape.
        evidence, reason = self.mod.classify(card)
        # filed_ts is real epoch seconds (unpinned) relative to actual now,
        # so this only proves dispatch, not the date math (covered above).
        self.assertTrue(evidence is not None or reason is not None)

    # -------------------------------------------------------------- test 7
    def test_env_threshold_3_heals_nothing_with_3_red_days(self):
        os.environ["FAILTASK_STUCK_DAYS"] = "3"
        try:
            rows = [row("k7", d) for d in (1, 5, 9)]  # 3 distinct red days, one yesterday
            rows += [row("other", d) for d in range(10, 30)]
            write_jsonl(self.mod.FAILURES_LOG, rows)
            evidence, reason = self.mod.check_stuck(stuck_card("k7", 20), now=NOW)
            self.assertIsNone(evidence, str((evidence, reason)))
        finally:
            del os.environ["FAILTASK_STUCK_DAYS"]

    def test_env_quiet_days_3_heals_a_4_day_quiet_key(self):
        os.environ["HEAL_STUCK_QUIET_DAYS"] = "3"
        try:
            rows = [row("k8", d) for d in (4, 6, 8, 10)]
            rows += [row("other", d) for d in range(11, 30)]
            write_jsonl(self.mod.FAILURES_LOG, rows)
            evidence, reason = self.mod.check_stuck(stuck_card("k8", 20), now=NOW)
            self.assertIsNotNone(evidence, reason)
        finally:
            del os.environ["HEAL_STUCK_QUIET_DAYS"]


if __name__ == "__main__":
    unittest.main()
