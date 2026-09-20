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

    def test_quiet_exactly_7_days_ago_does_not_heal(self):
        """Cold-review finding: 'no row in the last N full days' must count
        a row exactly N days old as still inside the window, the boundary
        the original formula (today - (N-1)) got wrong by one day. Density
        pinned AT the threshold (5, not below) so only the quiet boundary
        decides."""
        rows = [row("k9", d) for d in (7, 9, 10, 11, 12)]  # 5 distinct days
        rows += [row("other", d) for d in range(13, 30)]   # span padding only
        write_jsonl(self.mod.FAILURES_LOG, rows)
        evidence, reason = self.mod.check_stuck(stuck_card("k9", 20), now=NOW)
        self.assertIsNone(evidence, str((evidence, reason)))
        self.assertIn("still red", reason)

    def test_mutation_quiet_boundary_off_by_one(self):
        """Proof the fixed boundary formula actually catches a regression:
        narrowing quiet_start by one day (reverting toward the old,
        off-by-one formula) wrongly heals the exactly-7-days-ago case."""
        rows = [row("k9", d) for d in (7, 9, 10, 11, 12)]
        rows += [row("other", d) for d in range(13, 30)]
        write_jsonl(self.mod.FAILURES_LOG, rows)
        import inspect
        src = inspect.getsource(self.mod.check_stuck)
        marker = "quiet_start = today - timedelta(days=quiet_days_raw)"
        self.assertIn(marker, src, "quiet boundary line not found to mutate")
        mutated = src.replace(
            marker, "quiet_start = today - timedelta(days=quiet_days_raw - 1)", 1)
        self.assertNotEqual(src, mutated, "mutation target string not found")
        ns = dict(self.mod.__dict__)
        exec(compile(mutated, "<mutated check_stuck>", "exec"), ns)
        evidence, reason = ns["check_stuck"](stuck_card("k9", 20), now=NOW)
        self.assertIsNotNone(evidence, "mutation was not caught: still refused to heal")

    # -------------------------------------------------------------- test 2
    def test_density_3_of_14_with_row_yesterday_de_escalates_not_heals(self):
        # A key with a row yesterday is still active, so closing this card
        # must say "de-escalated", never "healed".
        rows = [row("k2", d) for d in (1, 8, 13)]  # 3 distinct days, most recent yesterday
        rows += [row("other", d) for d in range(14, 30)]  # pads the log span only
        write_jsonl(self.mod.FAILURES_LOG, rows)
        evidence, reason = self.mod.check_stuck(stuck_card("k2", 20), now=NOW)
        self.assertIsNotNone(evidence, reason)
        self.assertIn("3 red day", evidence)
        self.assertIn("de-escalated", evidence)
        self.assertNotIn("healed:", evidence)

    def test_density_one_under_the_threshold_stays_open_so_a_hovering_key_does_not_flap(self):
        # 4 of 14 is what a failure firing every few days looks like on its
        # quieter weeks; failtask would reopen the card at 5, so it stays open.
        rows = [row("k2", d) for d in (1, 4, 8, 13)]
        rows += [row("other", d) for d in range(14, 30)]
        write_jsonl(self.mod.FAILURES_LOG, rows)
        evidence, reason = self.mod.check_stuck(stuck_card("k2", 20), now=NOW)
        self.assertIsNone(evidence, str((evidence, reason)))
        self.assertIn("still red", reason)

    def test_margin_zero_closes_as_soon_as_density_is_under_the_threshold(self):
        rows = [row("k2", d) for d in (1, 4, 8, 13)]
        rows += [row("other", d) for d in range(14, 30)]
        write_jsonl(self.mod.FAILURES_LOG, rows)
        os.environ["HEAL_STUCK_DENSITY_MARGIN"] = "0"
        try:
            evidence, reason = self.mod.check_stuck(stuck_card("k2", 20), now=NOW)
        finally:
            os.environ.pop("HEAL_STUCK_DENSITY_MARGIN", None)
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
        closes the 4-of-14 case the margin exists to keep open."""
        rows = [row("k2", d) for d in (1, 4, 8, 13)]
        rows += [row("other", d) for d in range(14, 30)]
        write_jsonl(self.mod.FAILURES_LOG, rows)
        import inspect
        src = inspect.getsource(self.mod.check_stuck)
        mutated = src.replace(
            "density_ok = len(density_days) < threshold - heal_stuck_density_margin()",
            "density_ok = len(density_days) <= threshold - heal_stuck_density_margin()", 1)
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

    def test_partly_unreadable_log_refuses_not_heals(self):
        """Cold-review finding: a log whose tail is garbled (an interrupted
        append, a concurrent-write interleave) is indistinguishable from a
        genuine stop to the oldest-row span guard alone, since the guard
        only looks at the oldest row. Any skipped line at all must refuse
        to heal, naming the log and the skip count."""
        good_rows = [row("k12", d) for d in (8, 9, 10, 11, 12)]
        good_rows += [row("k12", d) for d in range(13, 30)]
        lines = [json.dumps(r) for r in good_rows]
        lines.append("{not valid json")
        lines.append(json.dumps({"key": "k12", "project": "infra", "title": "no ts"}))
        with open(self.mod.FAILURES_LOG, "w") as f:
            f.write("\n".join(lines) + "\n")
        evidence, reason = self.mod.check_stuck(stuck_card("k12", 20), now=NOW)
        self.assertIsNone(evidence, str((evidence, reason)))
        self.assertIn(self.mod.FAILURES_LOG, reason)
        self.assertIn("unparseable", reason)

    def test_zero_rows_for_key_does_not_heal(self):
        """Cold-review finding: a card exists only because failtask saw this
        key fire. A log with zero rows for it anywhere (not even outside
        the judged window) is evidence the log lost the key's history, not
        evidence the key stopped."""
        rows = [row("other", d) for d in range(0, 30)]
        write_jsonl(self.mod.FAILURES_LOG, rows)
        evidence, reason = self.mod.check_stuck(stuck_card("k11", 20), now=NOW)
        self.assertIsNone(evidence, str((evidence, reason)))
        self.assertIn("no rows anywhere", reason)

    # -------------------------------------------------------------- test 5
    def test_bookkeeping_row_does_not_count_as_firing(self):
        good_rows = [row("k5", d) for d in (8, 9, 10, 11, 12)]
        good_rows += [row("k5", d) for d in range(13, 30)]
        lines = [json.dumps(r) for r in good_rows]
        lines.append(json.dumps({"ts": ts_days_ago(1), "key": "stuck:k5",
                                  "project": "infra", "title": "x", "label": "STANDING"}))
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

    def test_env_quiet_days_changes_the_outcome(self):
        # Cold-review finding: the original fixture (4 distinct red days)
        # was vacuous: density alone (4 < default threshold 5) always
        # healed it regardless of the quiet knob. Pinned at 5 red days (AT
        # the threshold, so density_ok is False either way) so only the
        # quiet knob decides, and both halves are asserted in one test.
        rows = [row("k8", d) for d in (4, 6, 8, 10, 12)]  # 5 distinct days
        rows += [row("other", d) for d in range(13, 30)]
        write_jsonl(self.mod.FAILURES_LOG, rows)

        os.environ["HEAL_STUCK_QUIET_DAYS"] = "3"
        try:
            evidence, reason = self.mod.check_stuck(stuck_card("k8", 20), now=NOW)
            self.assertIsNotNone(evidence, reason)
        finally:
            del os.environ["HEAL_STUCK_QUIET_DAYS"]

        # Without the knob (default 7), the same fixture must NOT heal: the
        # most recent row (4 days ago) is inside the default 7-day window.
        evidence, reason = self.mod.check_stuck(stuck_card("k8", 20), now=NOW)
        self.assertIsNone(evidence, str((evidence, reason)))

    def test_env_quiet_days_0_disables_quiet_path_not_all_healing(self):
        """Cold-review finding: HEAL_STUCK_QUIET_DAYS<=0 must not become a
        closes-everything switch. failtask documents 0 as its OWN pause
        value for the sibling knob, so 0 here must mean 'disable the quiet
        path', never 'shrink the window to nothing'. A key firing every day
        including today, with density at the max (14 of 14), must not
        close."""
        rows = [row("k10", d) for d in range(0, 14)]  # every day, including today
        write_jsonl(self.mod.FAILURES_LOG, rows)
        os.environ["HEAL_STUCK_QUIET_DAYS"] = "0"
        try:
            evidence, reason = self.mod.check_stuck(stuck_card("k10", 20), now=NOW)
            self.assertIsNone(evidence, str((evidence, reason)))
        finally:
            del os.environ["HEAL_STUCK_QUIET_DAYS"]
        # -1 must behave the same as 0.
        os.environ["HEAL_STUCK_QUIET_DAYS"] = "-1"
        try:
            self.mod._failures_log_cache = None
            evidence, reason = self.mod.check_stuck(stuck_card("k10", 20), now=NOW)
            self.assertIsNone(evidence, str((evidence, reason)))
        finally:
            del os.environ["HEAL_STUCK_QUIET_DAYS"]

    # ------------------------------------------------------ future-dated row
    def test_future_dated_row_refuses_to_heal(self):
        """Cold-review finding: a row dated after today (clock skew, a bad
        UTC offset) is invisible to both windows, which would otherwise let
        the evidence string claim '0 red days' in the same breath as naming
        that future row."""
        rows = [row("k13", -1)]  # dated tomorrow
        rows += [row("other", d) for d in range(0, 30)]
        write_jsonl(self.mod.FAILURES_LOG, rows)
        evidence, reason = self.mod.check_stuck(stuck_card("k13", 20), now=NOW)
        self.assertIsNone(evidence, str((evidence, reason)))
        self.assertIn("future-dated", reason)


class CloseStampVerbTests(unittest.TestCase):
    def _stamp_for(self, evidence):
        mod = load_heal_sweep()
        mod.DRYRUN = True
        lines = []
        mod.log = lines.append
        card = {"repository": "o/r", "number": 1, "ref": "1", "item_id": "PVTI_x"}
        self.assertTrue(mod.close_card("/usr/bin/true", card, evidence))
        return lines[-1]

    def test_a_de_escalation_is_not_stamped_healed(self):
        line = self._stamp_for("stuck:k de-escalated: no longer standing; last row at X")
        self.assertIn("closed by heal-sweep: ", line)
        self.assertNotIn("healed: ", line)

    def test_a_real_heal_keeps_the_healed_stamp(self):
        line = self._stamp_for("stuck:k healed: last row at X")
        self.assertIn(": healed: ", line)


class HealSweepDryRunPulseLogTests(unittest.TestCase):
    def test_dry_run_report_line_is_marked(self):
        """Cold-review finding: --dry-run's final summary line in pulse.log
        must carry the same [dry-run] marker log() uses, so a rehearsal run
        is never indistinguishable from a real close in the pulse log."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mod = load_heal_sweep()
        mod.PULSE_LOG = os.path.join(tmp.name, "pulse.log")
        mod.DRYRUN = True
        mod.find_gh = lambda: "/usr/bin/true"
        mod.load_open_failure_cards = lambda: []
        mod.main()
        with open(mod.PULSE_LOG) as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        self.assertTrue(lines, "pulse.log got no lines written")
        self.assertIn("[dry-run]", lines[-1],
                       "dry-run report line missing the [dry-run] marker: %r" % lines[-1])


if __name__ == "__main__":
    unittest.main()
