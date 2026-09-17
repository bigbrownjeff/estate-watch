#!/usr/bin/env python3
"""Offline tests for heal-sweep's burn heal class (T-burn: burn:agent:<id> /
burn:agent-rollup:<date>).

heal-sweep.py's filename has a hyphen, so it isn't importable by name; this
harness loads it by path via importlib. All transcript fixtures live under a
tempdir and heal-sweep's PROFILE_HOME is pointed at that tempdir before any
call, so no test ever reads the real ~/.claude* trees.

Run: python3 local-tools/tests/test_heal_sweep_burn.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HEAL_SWEEP_PATH = os.path.join(os.path.dirname(HERE), "heal-sweep.py")


def load_heal_sweep():
    """Fresh module object each call (module-level PROFILE_HOME is mutated
    per test, and a fresh load also picks up any on-disk mutation-proof
    edit without needing a reload dance)."""
    spec = importlib.util.spec_from_file_location("heal_sweep_burn_test", HEAL_SWEEP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("ok   - %s" % name)
    else:
        FAIL += 1
        print("FAIL - %s%s" % (name, (": " + detail) if detail else ""))


def write_jsonl(path, messages):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for m in messages:
            f.write(json.dumps(m) + "\n")


def assistant_msg(content):
    return {"type": "assistant", "message": {"content": content}}


STRUCTURED_END = [assistant_msg([{"type": "text", "text": "wrapping up"}]),
                   assistant_msg([{"type": "tool_use", "name": "StructuredOutput", "input": {}}])]
TEXT_END = [assistant_msg([{"type": "tool_use", "name": "Read", "input": {}}]),
            assistant_msg([{"type": "text", "text": "All done, task complete."}])]
SESSION_LIMIT_END = [assistant_msg([{"type": "text", "text": "Looks like you hit your usage limit for now."}])]
MID_WORK_END = [assistant_msg([{"type": "text", "text": "working on it"}]),
                assistant_msg([{"type": "tool_use", "name": "Bash", "input": {}}])]


def card_for(agent_id, profile="claude-claudette", transcript_rel=None, filed_ts=None):
    rel = transcript_rel if transcript_rel is not None else (
        "sess1/subagents/workflows/wf1/agent-%s.jsonl" % agent_id)
    body = ("- session: sess1   profile: %s (jeffpinto.com)\n"
            "- transcript: %s\n" % (profile, rel))
    return {"failkey": "burn:agent:%s" % agent_id, "filed_ts": filed_ts, "body": body}


def set_mtime(path, hours_ago):
    ts = time.time() - hours_ago * 3600
    os.utime(path, (ts, ts))


def test_delivered_structured_heals():
    with tempfile.TemporaryDirectory() as tmp:
        mod = load_heal_sweep()
        mod.PROFILE_HOME = tmp
        path = os.path.join(tmp, ".claude-claudette", "projects",
                             "sess1/subagents/workflows/wf1/agent-a1.jsonl")
        write_jsonl(path, STRUCTURED_END)
        set_mtime(path, 1.0)
        evidence, reason = mod.classify_burn_agent(card_for("a1"))
        check("structured-output ending heals", evidence is not None and reason is None, str((evidence, reason)))
        check("evidence names delivered", evidence is not None and "delivered" in evidence, str(evidence))


def test_delivered_text_heals():
    with tempfile.TemporaryDirectory() as tmp:
        mod = load_heal_sweep()
        mod.PROFILE_HOME = tmp
        path = os.path.join(tmp, ".claude-claudette", "projects",
                             "sess1/subagents/workflows/wf1/agent-a2.jsonl")
        write_jsonl(path, TEXT_END)
        set_mtime(path, 2.0)
        evidence, reason = mod.classify_burn_agent(card_for("a2"))
        check("text-only ending (no trailing tool_use) heals", evidence is not None and reason is None, str((evidence, reason)))


def test_session_limit_stays_open():
    with tempfile.TemporaryDirectory() as tmp:
        mod = load_heal_sweep()
        mod.PROFILE_HOME = tmp
        path = os.path.join(tmp, ".claude-claudette", "projects",
                             "sess1/subagents/workflows/wf1/agent-a3.jsonl")
        write_jsonl(path, SESSION_LIMIT_END)
        set_mtime(path, 1.0)
        evidence, reason = mod.classify_burn_agent(card_for("a3"))
        check("session-limit death stays open", evidence is None, str((evidence, reason)))
        check("reason names session-limit", reason is not None and "session-limit" in reason, str(reason))


def test_mid_work_stays_open():
    with tempfile.TemporaryDirectory() as tmp:
        mod = load_heal_sweep()
        mod.PROFILE_HOME = tmp
        path = os.path.join(tmp, ".claude-claudette", "projects",
                             "sess1/subagents/workflows/wf1/agent-a4.jsonl")
        write_jsonl(path, MID_WORK_END)
        set_mtime(path, 1.0)
        evidence, reason = mod.classify_burn_agent(card_for("a4"))
        check("mid-work (non-StructuredOutput tool_use) ending stays open", evidence is None, str((evidence, reason)))
        check("reason names mid-work", reason is not None and "mid-work" in reason, str(reason))


def test_fresh_mtime_stays_open():
    with tempfile.TemporaryDirectory() as tmp:
        mod = load_heal_sweep()
        mod.PROFILE_HOME = tmp
        path = os.path.join(tmp, ".claude-claudette", "projects",
                             "sess1/subagents/workflows/wf1/agent-a5.jsonl")
        write_jsonl(path, STRUCTURED_END)
        set_mtime(path, 0.05)  # 3 minutes old, well under the 30min floor
        evidence, reason = mod.classify_burn_agent(card_for("a5"))
        check("fresh (<30min) transcript stays open even when delivered", evidence is None, str((evidence, reason)))
        check("reason names freshness", reason is not None and "fresh" in reason, str(reason))


def test_missing_transcript_stays_open():
    with tempfile.TemporaryDirectory() as tmp:
        mod = load_heal_sweep()
        mod.PROFILE_HOME = tmp
        evidence, reason = mod.classify_burn_agent(card_for("a6-nonexistent"))
        check("missing transcript stays open", evidence is None, str((evidence, reason)))
        check("reason names not-found", reason is not None and "not found" in reason, str(reason))


def test_glob_fallback_finds_moved_transcript():
    """The body's transcript: line can go stale (moved/renamed session dir);
    resolve_agent_transcript falls back to a glob on agent-<id>.jsonl."""
    with tempfile.TemporaryDirectory() as tmp:
        mod = load_heal_sweep()
        mod.PROFILE_HOME = tmp
        real_path = os.path.join(tmp, ".claude-claudine", "projects",
                                  "other-sess/subagents/workflows/wf9/agent-a7.jsonl")
        write_jsonl(real_path, STRUCTURED_END)
        set_mtime(real_path, 1.0)
        card = card_for("a7", profile="claude-claudette", transcript_rel="sess1/stale/agent-a7.jsonl")
        evidence, reason = mod.classify_burn_agent(card)
        check("glob fallback resolves a moved transcript", evidence is not None and reason is None, str((evidence, reason)))


def rollup_card(date_str, filed_ts):
    return {"failkey": "burn:agent-rollup:%s" % date_str, "filed_ts": filed_ts,
            "body": "briefs: wf1, wf2\nworkflow ids: wfid1, wfid2\n"}


def big_content(n_tool_use_msgs):
    msgs = []
    for _ in range(n_tool_use_msgs):
        msgs.append(assistant_msg([{"type": "tool_use", "name": "Read", "input": {}}]))
    msgs.append(assistant_msg([{"type": "tool_use", "name": "StructuredOutput", "input": {}}]))
    return msgs


def test_rollup_heals_when_all_delivered():
    with tempfile.TemporaryDirectory() as tmp:
        mod = load_heal_sweep()
        mod.PROFILE_HOME = tmp
        filed_ts = time.time() - 3600  # card filed 1h ago
        for i, aid in enumerate(("r1", "r2")):
            path = os.path.join(tmp, ".claude-claudette", "projects", "sessR/agent-%s.jsonl" % aid)
            write_jsonl(path, big_content(45))
            set_mtime(path, 2.0)  # idle, and inside the window (filed 1h ago, +2h pad)
        evidence, reason = mod.classify_burn_rollup(rollup_card("2026-09-16", filed_ts))
        check("rollup heals when every >40-tool_use transcript delivered", evidence is not None and reason is None, str((evidence, reason)))
        check("rollup evidence names the count", evidence is not None and "2 subagent" in evidence, str(evidence))


def test_rollup_open_when_one_session_limit():
    with tempfile.TemporaryDirectory() as tmp:
        mod = load_heal_sweep()
        mod.PROFILE_HOME = tmp
        filed_ts = time.time() - 3600
        good_path = os.path.join(tmp, ".claude-claudette", "projects", "sessR/agent-r3.jsonl")
        write_jsonl(good_path, big_content(45))
        set_mtime(good_path, 2.0)
        bad_msgs = [assistant_msg([{"type": "tool_use", "name": "Read", "input": {}}])] * 45
        bad_msgs.append(SESSION_LIMIT_END[0])
        bad_path = os.path.join(tmp, ".claude-claudette", "projects", "sessR/agent-r4.jsonl")
        write_jsonl(bad_path, bad_msgs)
        set_mtime(bad_path, 2.0)
        evidence, reason = mod.classify_burn_rollup(rollup_card("2026-09-16", filed_ts))
        check("rollup stays open when one transcript is a session-limit death", evidence is None, str((evidence, reason)))
        check("reason names the bad agent id", reason is not None and "r4=session-limit" in reason, str(reason))


def test_rollup_open_when_below_threshold():
    with tempfile.TemporaryDirectory() as tmp:
        mod = load_heal_sweep()
        mod.PROFILE_HOME = tmp
        filed_ts = time.time() - 3600
        path = os.path.join(tmp, ".claude-claudette", "projects", "sessR/agent-r5.jsonl")
        write_jsonl(path, big_content(10))  # only 11 tool_use msgs, under the 40 floor
        set_mtime(path, 2.0)
        evidence, reason = mod.classify_burn_rollup(rollup_card("2026-09-16", filed_ts))
        check("rollup stays open when no transcript exceeds the 40-tool_use floor", evidence is None, str((evidence, reason)))


def test_class_of_buckets_burn():
    mod = load_heal_sweep()
    check("class_of(burn:agent:x) is burn", mod.class_of("burn:agent:x") == "burn")
    check("class_of(burn:agent-rollup:2026-09-16) is burn", mod.class_of("burn:agent-rollup:2026-09-16") == "burn")


def test_classify_dispatches_to_burn():
    with tempfile.TemporaryDirectory() as tmp:
        mod = load_heal_sweep()
        mod.PROFILE_HOME = tmp
        path = os.path.join(tmp, ".claude-claudette", "projects",
                             "sess1/subagents/workflows/wf1/agent-a8.jsonl")
        write_jsonl(path, STRUCTURED_END)
        set_mtime(path, 1.0)
        evidence, reason = mod.classify(card_for("a8"))
        check("classify() dispatches burn:agent to the burn class", evidence is not None and reason is None, str((evidence, reason)))


def test_load_open_failure_cards_keeps_body():
    mod = load_heal_sweep()
    import inspect
    src = inspect.getsource(mod.load_open_failure_cards)
    check("load_open_failure_cards keeps body on the card dict", '"body": body' in src, src)


if __name__ == "__main__":
    test_delivered_structured_heals()
    test_delivered_text_heals()
    test_session_limit_stays_open()
    test_mid_work_stays_open()
    test_fresh_mtime_stays_open()
    test_missing_transcript_stays_open()
    test_glob_fallback_finds_moved_transcript()
    test_rollup_heals_when_all_delivered()
    test_rollup_open_when_one_session_limit()
    test_rollup_open_when_below_threshold()
    test_class_of_buckets_burn()
    test_classify_dispatches_to_burn()
    test_load_open_failure_cards_keeps_body()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
