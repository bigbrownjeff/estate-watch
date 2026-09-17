#!/usr/bin/env python3
"""turn-cap-gate sees Workflow arms, stops lanes it cannot find, and never blocks a report.

Runs the hook as Claude Code does: a JSON payload on stdin, exit 2 means deny.
HOME is a temp dir, so the real ~/.claude/state is never touched. Run directly:
    python3 local-tools/tests/test_turn_cap_gate.py [path/to/turn-cap-gate.py]
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "hooks", "turn-cap-gate.py")
PASSED = FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
        print("FAIL: %s %s" % (name, detail))


def tool_line():
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {}}]}}) + "\n"


def write_transcript(path, n_tool_turns):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(tool_line() * n_tool_turns)


def run(home, sess_path, agent_id, tool, command=None):
    payload = {"transcript_path": sess_path, "session_id": "sess1", "agent_id": agent_id,
               "tool_name": tool, "tool_input": {"command": command} if command else {}}
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("LANE_TURN", "WORKFLOW_ARM", "TURN_CAP"))}
    env["HOME"] = home
    r = subprocess.run([sys.executable, HOOK], input=json.dumps(payload), env=env,
                       capture_output=True, text=True, timeout=30)
    return r.returncode


def main():
    with tempfile.TemporaryDirectory() as home:
        proj = os.path.join(home, "projects", "-p")
        sess_path = os.path.join(proj, "sess1.jsonl")
        os.makedirs(proj)
        open(sess_path, "w").close()
        sub = os.path.join(proj, "sess1", "subagents")

        write_transcript(os.path.join(sub, "workflows", "wf_x", "agent-arm45.jsonl"), 45)
        check("a workflow arm past its cap is denied", run(home, sess_path, "arm45", "Read") == 2)
        check("a capped arm may still return StructuredOutput",
              run(home, sess_path, "arm45", "StructuredOutput") == 0)

        write_transcript(os.path.join(sub, "workflows", "wf_x", "agent-arm10.jsonl"), 10)
        check("a workflow arm under its cap is allowed", run(home, sess_path, "arm10", "Read") == 0)

        write_transcript(os.path.join(sub, "workflows", "wf_x", "agent-arm32.jsonl"), 32)
        check("an arm gets the arm cap (40), not the lane cap (25)",
              run(home, sess_path, "arm32", "Read") == 0)

        write_transcript(os.path.join(sub, "agent-lane30.jsonl"), 30)
        check("a flat lane past its cap is still denied", run(home, sess_path, "lane30", "Read") == 2)
        check("a capped lane may still hand back its report",
              run(home, sess_path, "lane30", "SubagentHandback") == 0)

        codes = [run(home, sess_path, "ghost", "Read") for _ in range(32)]
        check("a lane with no transcript anywhere is counted by its calls and stopped",
              codes[0] == 0 and codes[-1] == 2, str(codes))
    print("%d passed, %d failed" % (PASSED, FAILED))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
