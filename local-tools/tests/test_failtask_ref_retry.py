#!/usr/bin/env python3
"""Offline test: failtask retries board-ref assign (bounded) so cards get a Ref.
Reuses the heredoc-extraction harness of test_failtask_issue_path.py.
Run: python3 local-tools/tests/test_failtask_ref_retry.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_failtask_issue_path as h

FR = h.FakeResult


def run_case(fail_first):
    state = {"n": 0}

    def dispatch(cmd):
        if any("board-ref" in c for c in cmd):
            state["n"] += 1
            return FR(0) if state["n"] > fail_first else FR(1, stderr="transient")
        return None
    ns, calls = h.load_module(dispatch=dispatch)
    slept = []
    ns["time"] = type("T", (), {"sleep": staticmethod(slept.append), "time": staticmethod(__import__("time").time)})
    ok = ns["assign_ref"]("PVTI_x")
    return ok, state["n"], slept


ok, n, slept = run_case(2)
h.check("transient failures twice then success: assigned", ok is True)
h.check("three attempts made", n == 3, str(n))
h.check("backed off between attempts", len(slept) == 2, str(slept))

ok, n, slept = run_case(99)
h.check("persistent failure: returns False, never raises", ok is False)
h.check("bounded at 3 attempts", n == 3, str(n))

ok, n, _ = run_case(0)
h.check("first-try success makes one call", ok is True and n == 1, str(n))

print("\n%d passed, %d failed" % (h.PASS, h.FAIL))
sys.exit(1 if h.FAIL else 0)
