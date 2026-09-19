#!/usr/bin/env python3
"""burn-meter does not re-hand an unchanged turn-cap breach to failtask.

heal-sweep closes a delivered agent's card while the agent is still inside
burn-meter's 24h window; handing the same evidence to failtask again reopened
the card or, with a blind board cache, filed a duplicate (2026-09-17: five
agents filed at 12:00 and again at 20:00). Run directly:
    python3 local-tools/tests/test_burn_meter_refile.py
BURN_METER_PATH overrides the module under test (to run it against another copy).
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PASSED = FAILED = 0
CFG = {"project": "infra", "agent_turn_cap": 40, "max_agent_cards": 2,
       "session_tokens_per_24h": 10**15, "fable_subagent_tokens_per_24h": 10**15}


def load():
    target = os.environ.get("BURN_METER_PATH") or os.path.join(HERE, "..", "burn-meter.py")
    spec = importlib.util.spec_from_file_location("burn_meter", target)
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


def agg_for(mod, agents):
    """agents: {agent_id: tool_turns} -> the aggregate find_breaches reads."""
    agg = {"by_agent": {}, "agent_info": {}, "by_session": {}, "session_label": {},
           "fable_main_vs_sub": {}, "fable_agents": {}}
    for aid, turns in agents.items():
        akey = "prof::proj/sess/subagents/agent-%s.jsonl" % aid
        b = mod.zero()
        b.update(tool_turns=turns, turns=turns, output=100, cache_read=1000)
        agg["by_agent"][akey] = b
        agg["agent_info"][akey] = {"is_subagent": True, "agent_id": aid,
                                   "description": "brief " + aid}
    return agg


def keys(breaches):
    return sorted(br["key"] for br in breaches)


def write_ledger(path, breaches):
    """What failtask appends for each call: key, title, detail."""
    with open(path, "a") as f:
        for br in breaches:
            f.write(json.dumps({"key": br["key"], "title": br["title"],
                                "detail": br["detail"], "dedupe_hit": False}) + "\n")


def main():
    mod = load()
    with tempfile.TemporaryDirectory() as tmp:
        ledger = os.path.join(tmp, "failures.jsonl")

        # Nothing on record: every breach files (loud default), missing file is not an error.
        run1 = mod.find_breaches(agg_for(mod, {"a1": 175, "a2": 160, "a3": 55, "a4": 48}), CFG, 24)
        day = [k for k in keys(run1) if "rollup" in k][0]
        to_file, same = mod.split_unchanged(run1, mod.filed_turns(ledger))
        check("first sighting files all", keys(to_file) == keys(run1) and not same,
              keys(to_file))
        write_ledger(ledger, to_file)

        # Card healed, same agents, same counts: nothing is handed to failtask again.
        run2 = mod.find_breaches(agg_for(mod, {"a1": 175, "a2": 160, "a3": 55, "a4": 48}), CFG, 24)
        to_file, same = mod.split_unchanged(run2, mod.filed_turns(ledger))
        check("unchanged evidence is not re-filed", to_file == [], keys(to_file))
        check("unchanged breaches are still reported", keys(same) == keys(run1), keys(same))

        # An agent's count grew: its card files (failtask reopens it); the rest stay quiet.
        run3 = mod.find_breaches(agg_for(mod, {"a1": 190, "a2": 160, "a3": 55, "a4": 48}), CFG, 24)
        to_file, _ = mod.split_unchanged(run3, mod.filed_turns(ledger))
        check("grown turn count files", keys(to_file) == ["burn:agent:a1"], keys(to_file))

        # A new agent joins the rollup: the rollup files.
        run4 = mod.find_breaches(agg_for(mod, {"a1": 175, "a2": 160, "a3": 55, "a4": 48, "a5": 41}),
                                 CFG, 24)
        to_file, _ = mod.split_unchanged(run4, mod.filed_turns(ledger))
        check("new rolled-up agent files the rollup", keys(to_file) == [day], keys(to_file))

        # A rolled-up agent that later tops the list with the same count is already known.
        run5 = mod.find_breaches(agg_for(mod, {"a3": 55, "a4": 48}), CFG, 24)
        to_file, _ = mod.split_unchanged(run5, mod.filed_turns(ledger))
        check("rolled-up agent promoted to its own card is known", to_file == [], keys(to_file))

        # A brand-new agent files; a comma-grouped count in an old title parses.
        with open(ledger, "a") as f:
            f.write(json.dumps({"key": "burn:agent:abig",
                                "title": "burn: agent ran 1,203 tool turns (cap 40)"}) + "\n")
            f.write("not json\n")
        known = mod.filed_turns(ledger)
        check("comma count parses", known.get("abig") == 1203, known.get("abig"))
        run6 = mod.find_breaches(agg_for(mod, {"abig": 1203, "anew": 44}), CFG, 24)
        to_file, _ = mod.split_unchanged(run6, known)
        check("new agent files, known one does not", keys(to_file) == ["burn:agent:anew"],
              keys(to_file))

        # Breaches with no evidence (session, fable) always pass through.
        passthru = [{"key": "burn:session:s1", "title": "t"}]
        to_file, _ = mod.split_unchanged(passthru, known)
        check("session breach unaffected", to_file == passthru)

    print("test_burn_meter_refile: %d passed, %d failed" % (PASSED, FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
