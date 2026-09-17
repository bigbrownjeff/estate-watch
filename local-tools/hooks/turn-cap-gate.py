#!/usr/bin/env python3
"""turn-cap-gate: PreToolUse hook that ENFORCES the lane turn cap on subagents.

Why: the cap ("stop at ~20 tool turns and report state") lived only as a sentence
in briefs. Agents do not count their own turns, and the burn-meter reports the
overrun the next morning. 2026-09-02: W4 leg 3 ran 65 turns / 401k tokens, the
#214 lane ~55, the claims-lint lane ~48, all in one session, all past the cap.

What it does, per tool call, for SUBAGENT transcripts only
(`.../<session>/subagents/agent-<id>.jsonl`; main sessions are interactive and
exempt; workflow arms sit under a `subagents/` tree too and are covered):
  turns < WARN            allow, silent
  WARN <= turns < CAP     allow, inject a countdown as additionalContext
  CAP <= turns < HARD     allow ONLY `git add|commit|push|status|diff|log` so the
                          lane can persist its state; deny everything else
  turns >= HARD           deny every tool call
A denial is exit 2 with the instruction on stderr, which Claude Code feeds back
to the agent: stop, report done / remaining / exact next command.

Counting is incremental: one JSON cache per transcript under
~/.claude/state/turn-cap/, holding the byte offset consumed and the tool_use count,
so each call reads only the appended bytes. A `tool_use` content block in an
`assistant` line is one turn (same definition as burn-meter's tool_turns).

Overrides (per session, via the environment the session was launched with):
  LANE_TURN_CAP  (default 25)   LANE_TURN_WARN (default 18)   LANE_TURN_HARD (CAP+4)
  TURN_CAP_GATE=off             disables the gate entirely
Anything unexpected (bad JSON, unreadable transcript) exits 0: the gate fails
open, because only a deliberate deny may ever block a tool call.
"""
import glob
import hashlib
import json
import os
import re
import sys
import time

STATE_DIR = os.path.expanduser("~/.claude/state/turn-cap")
LEDGER = os.path.join(STATE_DIR, "lanes.tsv")

REPORT_TOOLS = ("SubagentHandback", "StructuredOutput")
GIT_PERSIST = re.compile(
    r"^\s*(cd\s+\S+\s*(&&|;)\s*)?git\s+(-C\s+\S+\s+)?"
    r"(add|commit|push|status|diff|log|stash|worktree\s+list)\b"
)


def env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def count_new_turns(path, cache):
    """Count tool_use blocks appended since cache['offset']; return (count, offset)."""
    size = os.path.getsize(path)
    offset = cache.get("offset", 0)
    count = cache.get("count", 0)
    if size < offset:  # rewritten or truncated: start over
        offset, count = 0, 0
    if size == offset:
        return count, offset
    with open(path, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read(size - offset)
    # consume only whole lines; a writer may be mid-line
    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:
        return count, offset
    for raw in chunk[: last_nl + 1].split(b"\n"):
        if not raw.strip():
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if d.get("type") != "assistant":
            continue
        content = (d.get("message") or {}).get("content") or []
        if isinstance(content, list):
            count += sum(
                1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use"
            )
    return count, offset + last_nl + 1


def lane_meta(path):
    mp = path[:-6] + ".meta.json" if path.endswith(".jsonl") else ""
    try:
        with open(mp) as fh:
            return json.load(fh)
    except Exception:
        return {}


def append_ledger(agent_id, meta, turns, verdict):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        new = not os.path.exists(LEDGER)
        with open(LEDGER, "a") as fh:
            if new:
                fh.write("ts\tagent\tagentType\tmodel\tturns\tverdict\tdescription\n")
            fh.write(
                "\t".join(
                    [
                        time.strftime("%Y-%m-%dT%H:%M:%S"),
                        agent_id,
                        str(meta.get("agentType", "")),
                        str(meta.get("model", "") or "INHERITED"),
                        str(turns),
                        verdict,
                        str(meta.get("description", ""))[:80].replace("\t", " "),
                    ]
                )
                + "\n"
            )
    except Exception:
        pass


def main():
    if os.environ.get("TURN_CAP_GATE", "").lower() == "off":
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    tp = payload.get("transcript_path") or ""
    # 2026-09-09, measured: inside a subagent the payload carries `agent_id` and `agent_type`,
    # but `transcript_path` is the PARENT session's transcript, so the old "/subagents/ in
    # transcript_path" test never matched and this gate had never fired for a real lane (the
    # ledger held no START row since it was built on 09-02). The subagent's own transcript is
    # <parent dir>/<session id>/subagents/agent-<agent_id>.jsonl; build it from agent_id.
    sub_id = payload.get("agent_id") or ""
    if not sub_id:
        return 0  # a main, interactive session: exempt
    if "/subagents/" not in tp:
        sess = payload.get("session_id") or re.sub(r"\.jsonl$", "", os.path.basename(tp))
        sub_root = os.path.join(os.path.dirname(tp), sess, "subagents")
        tp = os.path.join(sub_root, f"agent-{sub_id}.jsonl")
        if not os.path.isfile(tp):
            # 2026-09-17, measured: a Workflow arm's transcript is one level down, at
            # subagents/workflows/<wf id>/agent-<id>.jsonl. The flat path never existed for
            # an arm, the count sat at 1, and the gate never fired for one: 35 arms ran past
            # 40 tool turns in a day (the worst 363) under an "enforcing" hook.
            hits = glob.glob(os.path.join(sub_root, "**", f"agent-{sub_id}.jsonl"), recursive=True)
            if hits:
                tp = hits[0]
    if not tp:
        return 0
    is_arm = "/subagents/workflows/" in tp

    # Workflow arms are page-scale build and gate steps; they get the burn meter's cap (40).
    cap = env_int("WORKFLOW_ARM_TURN_CAP", 40) if is_arm else env_int("LANE_TURN_CAP", 25)
    warn = env_int("WORKFLOW_ARM_TURN_WARN", 30) if is_arm else env_int("LANE_TURN_WARN", 18)
    hard = env_int("LANE_TURN_HARD", cap + 4)

    os.makedirs(STATE_DIR, exist_ok=True)
    key = hashlib.sha1(tp.encode()).hexdigest()[:16]
    cache_path = os.path.join(STATE_DIR, f"{key}.json")
    cache = {}
    try:
        with open(cache_path) as fh:
            cache = json.load(fh)
    except Exception:
        cache = {}

    found = os.path.isfile(tp)
    if found:
        turns, offset = count_new_turns(tp, cache)
    else:  # the lane's very first call can precede its transcript file
        turns, offset = cache.get("count", 0), cache.get("offset", 0)
    turns += 1  # the call being gated right now
    try:
        with open(cache_path, "w") as fh:
            # With no transcript to read, count the calls themselves, so a lane the gate
            # cannot find is still a lane the gate can stop.
            json.dump({"offset": offset, "count": turns - 1 if found else turns, "path": tp,
                       "updated": time.time()}, fh)
    except Exception:
        pass

    agent_id = re.sub(r"\.jsonl$", "", os.path.basename(tp))
    meta = lane_meta(tp)
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    if not cache:
        # one START row per lane, on the first call this gate sees (the transcript may already
        # hold that call's tool_use line, so "turns == 1" missed it; the empty cache does not).
        # The model column is the RULE #1 audit trail (INHERITED means no explicit model).
        append_ledger(agent_id, meta, turns, "START")
    if turns < warn:
        return 0

    if turns < cap:
        left = cap - turns
        ctx = (
            f"[turn-cap-gate] Tool turn {turns} of {cap} for this lane. {left} left before "
            f"tool calls are denied. Start wrapping up: finish the current step, commit and "
            f"push, then report done / remaining / exact next command."
        )
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "additionalContext": ctx}}))
        if turns == warn:
            append_ledger(agent_id, meta, turns, "WARN")
        return 0

    if tool in REPORT_TOOLS:
        # Delivering the report is what the cap asks for; denying it strands the lane
        # (2026-09-15: six SubagentHandback calls blocked, coordinator got no report).
        append_ledger(agent_id, meta, turns, "CAP-report-allowed")
        return 0

    stop_msg = (
        f"[turn-cap-gate] TURN CAP REACHED: this lane has used {turns} tool turns "
        f"(cap {cap}). Do not call any more tools. Write your final report now: "
        f"(1) what is done, with paths and commit shas; (2) what remains; (3) the exact "
        f"next command. The coordinator re-spawns a fresh lane with that state."
    )
    if turns < hard and tool == "Bash" and GIT_PERSIST.match(str(tool_input.get("command", ""))):
        append_ledger(agent_id, meta, turns, "CAP-git-allowed")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "additionalContext": stop_msg +
                                                 " (This git call is allowed so state persists.)"}}))
        return 0

    append_ledger(agent_id, meta, turns, "DENY")
    if turns < hard:
        stop_msg += (
            f" Only `git add|commit|push|status` is still allowed until turn {hard}, "
            f"so uncommitted work can be saved first."
        )
    sys.stderr.write(stop_msg + "\n")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
