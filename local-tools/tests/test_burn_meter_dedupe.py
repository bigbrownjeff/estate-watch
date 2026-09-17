#!/usr/bin/env python3
"""burn-meter folds transcript lines by API message id.

Claude Code writes one JSONL line per content block, and every line of a
message carries that message's whole usage. Run directly:
    python3 local-tools/tests/test_burn_meter_dedupe.py
"""
import datetime
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PASSED = FAILED = 0


def load():
    spec = importlib.util.spec_from_file_location(
        "burn_meter", os.path.join(HERE, "..", "burn-meter.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
        print("FAIL: %s %s" % (name, detail))


def line(mid, block, usage, ts):
    return {"type": "assistant", "timestamp": ts,
            "message": {"id": mid, "model": "claude-test-1", "usage": usage, "content": [block]}}


def main():
    mod = load()
    now = datetime.datetime.now(datetime.timezone.utc)
    ts = now.isoformat().replace("+00:00", "Z")
    usage = {"input_tokens": 10, "output_tokens": 100,
             "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 50000}
    with tempfile.TemporaryDirectory() as tmp:
        sess = os.path.join(tmp, "projects", "-proj")
        os.makedirs(sess)
        rows = [
            # one message, three content blocks: text + two parallel tool calls
            line("msg_A", {"type": "text", "text": "plan"}, usage, ts),
            line("msg_A", {"type": "tool_use", "name": "Read", "input": {}}, usage, ts),
            line("msg_A", {"type": "tool_use", "name": "Grep", "input": {}}, usage, ts),
            # a second message, text only
            line("msg_B", {"type": "text", "text": "report"}, usage, ts),
        ]
        with open(os.path.join(sess, "11111111-aaaa.jsonl"), "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        mod.ROOTS = {"fixture": os.path.join(tmp, "projects")}
        cutoff = now - datetime.timedelta(hours=1)
        agg = mod.collect(cutoff, cutoff.timestamp())
        b = agg["by_model"]["claude-test-1"]
        check("two messages count as two turns, not four lines", b["turns"] == 2, str(dict(b)))
        check("cache read counted once per message", b["cache_read"] == 100000, str(dict(b)))
        check("output counted once per message", b["output"] == 200, str(dict(b)))
        check("parallel tool calls in one message are one tool turn", b["tool_turns"] == 1, str(dict(b)))
    print("%d passed, %d failed" % (PASSED, FAILED))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
