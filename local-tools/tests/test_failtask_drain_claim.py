#!/usr/bin/env python3
"""Offline tests for failtask's pending-queue drain: one process files each
queued row once, even when another caller arrives mid-drain without the lock.

Run: python3 ~/.claude/bin/tests/test_failtask_drain_claim.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_failtask_issue_path as h  # noqa: E402

GH = h.GH


def fresh(dispatch=None):
    ns, calls = h.load_module(dispatch=dispatch)
    d = tempfile.mkdtemp(prefix="failtask-drain-")
    ns["FAILDIR"] = d
    ns["PENDING"] = os.path.join(d, "pending-board.jsonl")
    ns["CACHE"] = os.path.join(d, "board-cache.json")
    ns["assign_ref"] = lambda item_id: None
    ns["write_cache"]([])
    return ns, calls, d


def row(key):
    return {"ts": "2026-11-12T01:00:00-05:00", "project": "infra", "title": "t " + key,
            "detail": "", "key": key, "severity": "error", "label": "FAILURE", "host": "h"}


def queue(ns, keys):
    with open(ns["PENDING"], "w") as f:
        for k in keys:
            f.write(json.dumps(row(k)) + "\n")


def creates(calls):
    return [c for c in calls if "issue" in c and "create" in c]


def test_a_second_caller_mid_drain_files_nothing():
    """The 2026-11-12 defect: a caller that arrives while a drain is running
    must find no queue to replay."""
    state = {"inner": None}
    ns, calls, d = fresh()
    real_create = ns["board_create"]

    def create_then_intrude(gh_bin, rec, items, tax):
        out = real_create(gh_bin, rec, items, tax)
        if state["inner"] is None:   # a second caller, own item list, mid-drain
            state["inner"] = -1      # set first: the intruder's own creates must not re-enter
            state["inner"] = ns["drain_pending"](GH, [], {})
        return out

    ns["board_create"] = create_then_intrude
    queue(ns, ["k1", "k2", "k3"])
    n = ns["drain_pending"](GH, [], {})
    h.check("a: outer drain files all three rows", n == 3, str(n))
    h.check("a: the intruding caller drains nothing", state["inner"] == 0, str(state["inner"]))
    h.check("a: exactly three issues created, not six", len(creates(calls)) == 3, str(len(creates(calls))))
    h.check("a: queue and claim are both gone", os.listdir(d) == ["board-cache.json"], str(os.listdir(d)))


def test_b_failed_rows_return_to_the_queue_once():
    def dispatch(cmd):
        joined = " ".join(cmd)
        if "t k2" in joined and ("issue create" in joined or "item-create" in joined):
            return h.FakeResult(1, stderr="rate limit")
        return None
    ns, calls, d = fresh(dispatch=dispatch)
    queue(ns, ["k1", "k2", "k3"])
    n = ns["drain_pending"](GH, [], {})
    left = [json.loads(l)["key"] for l in open(ns["PENDING"])]
    h.check("b: two rows drained", n == 2, str(n))
    h.check("b: only the failed row is back in the queue", left == ["k2"], str(left))
    h.check("b: no claim file left", not any("claim" in x for x in os.listdir(d)), str(os.listdir(d)))


def test_c_dead_claim_is_requeued_and_replay_dedupes():
    """A drain killed half way leaves its claim; the next caller returns it to
    the queue and the rows it had already filed dedupe against the cache."""
    ns, calls, d = fresh()
    queue(ns, ["k1", "k2"])
    items = []
    ns["board_create"](GH, row("k1"), items, {})   # the dead drain filed k1
    ns["cache_sync"](items)
    dead = os.path.join(d, "pending-board.claim.999999999.jsonl")
    os.rename(ns["PENDING"], dead)
    before = len(creates(calls))
    n = ns["drain_pending"](GH, ns["read_cache"](), {})
    h.check("c: both rows accounted for", n == 2, str(n))
    h.check("c: only k2 is created on replay", len(creates(calls)) - before == 1, str(len(creates(calls)) - before))
    h.check("c: dead claim removed", not os.path.exists(dead))


def test_d_live_claim_is_left_alone():
    ns, calls, d = fresh()
    live = os.path.join(d, "pending-board.claim.%d.jsonl" % os.getppid())
    with open(live, "w") as f:
        f.write(json.dumps(row("k9")) + "\n")
    n = ns["drain_pending"](GH, [], {})
    h.check("d: a live drain's claim is not taken", n == 0 and os.path.exists(live), str(os.listdir(d)))
    h.check("d: nothing created", len(creates(calls)) == 0)


def test_e_offline_noise_file_is_not_a_claim():
    ns, calls, d = fresh()
    noise = os.path.join(d, "pending-board.offline-noise-2026-11-08-to-12.jsonl")
    with open(noise, "w") as f:
        f.write(json.dumps(row("k8")) + "\n")
    n = ns["drain_pending"](GH, [], {})
    h.check("e: parked rows are never drained", n == 0 and os.path.exists(noise) and not creates(calls))


if __name__ == "__main__":
    test_a_second_caller_mid_drain_files_nothing()
    test_b_failed_rows_return_to_the_queue_once()
    test_c_dead_claim_is_requeued_and_replay_dedupes()
    test_d_live_claim_is_left_alone()
    test_e_offline_noise_file_is_not_a_claim()
    print("\n%d passed, %d failed" % (h.PASS, h.FAIL))
    sys.exit(1 if h.FAIL else 0)
