#!/usr/bin/env python3
"""burn-meter — token-burn watchdog over Claude Code transcripts.

Reads every session, subagent, and workflow-arm transcript modified in the last
N hours under all four Claude profiles' projects dirs, aggregates the
usage recorded in each assistant turn (message.model + message.usage), and
prints a compact report: totals per model, per profile, the biggest sessions and
agents, every agent that ran past the turn cap, every Fable turn that happened
outside a main loop (with the brief that spawned it), and the hourly rate.

It is the event-time answer to the 2026-08-27/28 forensic: two sessions burned
97% of two days' tokens and nothing noticed until a hand audit. Thresholds live
in ~/.claude/failures/sentinel-config.json under "burn"; a breach files a
failtask card (deduped per agent/session per day) so it lands on the board.

Read-only on transcripts. Writes only its own report under
~/.claude/failures/burn/ and, on a breach, a board card via failtask.

Usage:
  burn-meter.py [--hours N] [--since ISO] [--dry-run] [--quiet] [--no-file]
  --hours N     window, default from config (24)
  --since ISO   explicit local-time cutoff, overrides --hours
  --dry-run     print the cards that WOULD be filed, touch nothing
  --quiet       estate line only (fleet-sentinel's report mode still files cards)
  --no-file     never file cards, even outside dry-run
"""
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/New_York")
HOME = os.path.expanduser("~")
FAILDIR = os.path.join(HOME, ".claude", "failures")
CONFIG = os.path.join(FAILDIR, "sentinel-config.json")
BURN_DIR = os.path.join(FAILDIR, "burn")
FAILTASK = os.path.join(HOME, ".claude", "bin", "failtask")

ROOTS = {
    "claude (jeffpinto.com)": os.path.join(HOME, ".claude", "projects"),
    "claude-claudine (gmail)": os.path.join(HOME, ".claude-claudine", "projects"),
    # Four sister profiles since 2026-09-02 (Jeff, 09-07: "treated in parallel").
    # claudette ran the busiest sessions of the week and was invisible here
    # until the 2026-09-09 walk; the turn-cap watchdog must see every profile.
    "claude-claudette (jeffpinto.com)": os.path.join(HOME, ".claude-claudette", "projects"),
    "claude-claudeux (bluecamel)": os.path.join(HOME, ".claude-claudeux", "projects"),
}

DEFAULTS = {
    "window_hours": 24,
    "agent_turn_cap": 40,
    "agent_turn_warn": 20,
    "session_tokens_per_24h": 1_500_000_000,
    "fable_subagent_tokens_per_24h": 50_000_000,
    "report_keep": 30,
    "project": "infra",
}


def zero():
    return {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
            "turns": 0, "tool_turns": 0}


def total(b):
    return b["input"] + b["output"] + b["cache_write"] + b["cache_read"]


def human(n):
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n/div:.2f}{unit}"
    return str(int(n))


def parse_ts(ts):
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG) as f:
            burn = json.load(f).get("burn") or {}
        cfg.update({k: v for k, v in burn.items() if not k.startswith("_")})
    except Exception as e:
        sys.stderr.write(f"burn-meter: sentinel-config.json unreadable ({e}) — using defaults\n")
    return cfg


def classify(root_label, root_dir, filepath):
    """Where does this transcript sit: main session, direct subagent, workflow arm."""
    parts = os.path.relpath(filepath, root_dir).split(os.sep)
    is_sub = "subagents" in parts
    workflow_id = None
    if is_sub and "workflows" in parts:
        wi = parts.index("workflows")
        if wi + 1 < len(parts):
            workflow_id = parts[wi + 1]
    agent_id = None
    if is_sub:
        m = re.match(r"agent-([0-9a-f]+)\.jsonl$", parts[-1])
        if m:
            agent_id = m.group(1)
        session = parts[1]
    else:
        session = parts[-1][:-6] if parts[-1].endswith(".jsonl") else parts[-1]
    return {"profile": root_label, "project": parts[0], "is_subagent": is_sub,
            "workflow_id": workflow_id, "session": session, "agent_id": agent_id,
            "rel": os.sep.join(parts)}


def first_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                return c.get("text", "")
    return ""


def collect(cutoff_utc, cutoff_epoch):
    """Single pass over every candidate transcript. Returns the aggregate bundle."""
    agg = {
        "by_model": defaultdict(zero),
        "by_profile": defaultdict(zero),
        "by_session": defaultdict(zero),
        "by_agent": defaultdict(zero),
        "by_hour": defaultdict(zero),
        "by_persona": defaultdict(zero),
        "fable_main_vs_sub": defaultdict(zero),
    }
    session_label, agent_info, fable_agents = {}, {}, {}
    files_scanned = 0

    for label, root in ROOTS.items():
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, filenames in os.walk(root):
            for fn in filenames:
                if not fn.endswith(".jsonl") or fn == "journal.jsonl":
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    if os.stat(fp).st_mtime < cutoff_epoch:
                        continue
                except OSError:
                    continue
                files_scanned += 1
                cls = classify(label, root, fp)
                meta = {}
                if cls["is_subagent"]:
                    mp = fp[:-6] + ".meta.json"
                    if os.path.exists(mp):
                        try:
                            meta = json.load(open(mp))
                        except Exception:
                            meta = {}
                akey = f"{label}::{cls['rel']}"
                skey = (label, cls["session"])
                first_user = None
                models_seen = set()

                try:
                    fh = open(fp, "r", errors="replace")
                except OSError:
                    continue
                with fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        typ = d.get("type")
                        if typ == "user" and first_user is None:
                            txt = first_text((d.get("message") or {}).get("content"))
                            if txt and not txt.lstrip().startswith("<"):
                                first_user = " ".join(txt.split())[:90]
                        if typ != "assistant":
                            continue
                        msg = d.get("message") or {}
                        usage, model = msg.get("usage"), msg.get("model")
                        ts = parse_ts(d.get("timestamp"))
                        if not usage or not model or not ts or ts < cutoff_utc:
                            continue
                        if model == "<synthetic>":
                            continue  # placeholder turns, zero usage
                        inp = usage.get("input_tokens") or 0
                        out = usage.get("output_tokens") or 0
                        cw = usage.get("cache_creation_input_tokens") or 0
                        cr = usage.get("cache_read_input_tokens") or 0
                        is_tool_turn = any(
                            isinstance(c, dict) and c.get("type") == "tool_use"
                            for c in (msg.get("content") or [])
                            if isinstance(msg.get("content"), list)
                        )

                        def add(b):
                            b["input"] += inp
                            b["output"] += out
                            b["cache_write"] += cw
                            b["cache_read"] += cr
                            b["turns"] += 1
                            b["tool_turns"] += 1 if is_tool_turn else 0

                        add(agg["by_model"][model])
                        add(agg["by_profile"][label])
                        add(agg["by_session"][skey])
                        add(agg["by_agent"][akey])
                        add(agg["by_hour"][ts.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:00")])
                        persona = (d.get("attributionAgent") or meta.get("agentType")
                                   or "main-loop (no persona)")
                        add(agg["by_persona"][persona])
                        models_seen.add(model)
                        if "fable" in model.lower():
                            add(agg["fable_main_vs_sub"]["subagent" if cls["is_subagent"] else "main"])

                if not cls["is_subagent"]:
                    session_label[skey] = first_user or "(no user text found)"
                agent_info[akey] = {
                    **cls,
                    "description": meta.get("description") or (first_user or ""),
                    "agentType": meta.get("agentType"),
                    "model_meta": meta.get("model"),
                    "parentAgentId": meta.get("parentAgentId"),
                    "spawnDepth": meta.get("spawnDepth"),
                    "models": sorted(models_seen),
                }
                if cls["is_subagent"] and any("fable" in m.lower() for m in models_seen):
                    fable_agents[akey] = agent_info[akey]

    agg["session_label"] = session_label
    agg["agent_info"] = agent_info
    agg["fable_agents"] = fable_agents
    agg["files_scanned"] = files_scanned
    return agg


def agent_label(info):
    """Human label for an agent transcript: brief first, then what spawned it."""
    desc = info.get("description") or ""
    if not desc and info.get("workflow_id"):
        desc = "(workflow arm, no per-call description)"
    if not desc:
        desc = "(no description)"
    where = info.get("workflow_id") or ("direct spawn" if info["is_subagent"] else "main loop")
    return desc, where


def render(agg, cfg, cutoff_local, now_local, breaches):
    L = []
    w = L.append
    grand = zero()
    for b in agg["by_model"].values():
        for k in grand:
            grand[k] += b[k]
    gt = total(grand)
    hours = max((now_local - cutoff_local).total_seconds() / 3600, 0.01)

    w("=" * 78)
    w(f"BURN METER  {cutoff_local:%Y-%m-%d %H:%M} to {now_local:%Y-%m-%d %H:%M} "
      f"({hours:.1f}h, {agg['files_scanned']} transcripts)")
    w("=" * 78)
    fresh = grand["input"] + grand["output"] + grand["cache_write"]
    w(f"TOTAL {human(gt)} tokens over {grand['turns']:,} assistant turns "
      f"({grand['tool_turns']:,} tool turns) — {human(gt/hours)}/h")
    w(f"  fresh (in+out+cache-write) {human(fresh)} ({100*fresh/gt if gt else 0:.1f}%)   "
      f"cache-read {human(grand['cache_read'])} ({100*grand['cache_read']/gt if gt else 0:.1f}%)")
    w("")

    w("-- by model " + "-" * 66)
    w(f"{'model':<26}{'input':>10}{'output':>10}{'cache-w':>11}{'cache-r':>11}{'total':>11}{'share':>8}")
    for m, b in sorted(agg["by_model"].items(), key=lambda kv: -total(kv[1])):
        w(f"{m[:25]:<26}{human(b['input']):>10}{human(b['output']):>10}"
          f"{human(b['cache_write']):>11}{human(b['cache_read']):>11}"
          f"{human(total(b)):>11}{100*total(b)/gt if gt else 0:>7.1f}%")
    w("")

    w("-- by profile " + "-" * 64)
    for p, b in sorted(agg["by_profile"].items(), key=lambda kv: -total(kv[1])):
        w(f"{p:<30}{human(total(b)):>11}{100*total(b)/gt if gt else 0:>7.1f}%   {b['turns']:,} turns")
    w("")

    w("-- by persona / attributionAgent " + "-" * 45)
    for p, b in sorted(agg["by_persona"].items(), key=lambda kv: -total(kv[1]))[:12]:
        w(f"{p[:34]:<36}{human(total(b)):>11}{b['turns']:>9,} turns{b['output']:>12,} out")
    w("")

    w("-- top 10 sessions " + "-" * 58)
    for (prof, sid), b in sorted(agg["by_session"].items(), key=lambda kv: -total(kv[1]))[:10]:
        lab = agg["session_label"].get((prof, sid), "(subagent-only / unlabeled)")
        w(f"  {human(total(b)):>9}  {b['turns']:>6,}t  {sid[:8]}  {lab[:56]}")
    w("")

    cap, warn = cfg["agent_turn_cap"], cfg["agent_turn_warn"]
    subs = [(k, b) for k, b in agg["by_agent"].items()
            if agg["agent_info"].get(k, {}).get("is_subagent")]
    w("-- top 10 subagents by tokens " + "-" * 47)
    for akey, b in sorted(subs, key=lambda kv: -total(kv[1]))[:10]:
        info = agg["agent_info"].get(akey, {})
        desc, where = agent_label(info)
        w(f"  {human(total(b)):>9} {b['turns']:>6,}t  {','.join(info.get('models', []))[:16]:<17}{desc[:38]}")
        w(f"                        via {where}")
    w("")

    w("-- top 10 subagents by turns " + "-" * 48)
    for akey, b in sorted(subs, key=lambda kv: -kv[1]["turns"])[:10]:
        info = agg["agent_info"].get(akey, {})
        desc, _ = agent_label(info)
        w(f"  {b['turns']:>6,}t ({b['tool_turns']:>5,} tool) {human(total(b)):>9}  {desc[:44]}")
    w("")

    over = [(k, b) for k, b in agg["by_agent"].items()
            if agg["agent_info"].get(k, {}).get("is_subagent") and b["tool_turns"] >= warn]
    over.sort(key=lambda kv: -kv[1]["tool_turns"])
    w(f"-- agents over the {warn}-tool-turn warn line ({len(over)} found; cap={cap}) " + "-" * 20)
    if not over:
        w("  none")
    for akey, b in over[:40]:
        info = agg["agent_info"].get(akey, {})
        desc, where = agent_label(info)
        flag = "BREACH" if b["tool_turns"] >= cap else " warn "
        w(f"  [{flag}] {b['tool_turns']:>5,} tool turns  {human(total(b)):>9}  "
          f"{info.get('agent_id') or '?'}  {desc[:40]}")
        w(f"           via {where}, model {','.join(info.get('models', [])) or '?'}")
    if len(over) > 40:
        w(f"  ... and {len(over)-40} more over the warn line")
    w("")

    fmain = total(agg["fable_main_vs_sub"].get("main", zero()))
    fsub = total(agg["fable_main_vs_sub"].get("subagent", zero()))
    w("-- Fable outside a main loop (RULE #1) " + "-" * 39)
    w(f"  Fable total {human(fmain+fsub)} — main loop {human(fmain)} (allowed), "
      f"subagent {human(fsub)} (needs Jeff's approval per spawn)")
    fa = sorted(((k, agg["by_agent"][k]) for k in agg["fable_agents"]),
                key=lambda kv: -total(kv[1]))
    if not fa:
        w("  no Fable subagent turns in window")
    for akey, b in fa:
        info = agg["fable_agents"][akey]
        desc, where = agent_label(info)
        w(f"  {human(total(b)):>9}  {b['turns']:>5,}t  {where:<22} {desc[:44]}")
        if info.get("model_meta"):
            w(f"             meta model={info['model_meta']} (spawn did not override to Sonnet/Opus)")
    w("")

    w("-- hourly rate " + "-" * 63)
    for hour, b in sorted(agg["by_hour"].items()):
        t = total(b)
        bar = "#" * min(int(40 * t / max(gt, 1) * 8), 40)
        w(f"  {hour}  {human(t):>9}  {b['turns']:>5,}t  {bar}")
    w("")

    w("-- thresholds " + "-" * 64)
    if not breaches:
        w("  no threshold breached")
    for br in breaches:
        w(f"  [{br['severity'].upper()}] {br['title']}")
    w("=" * 78)
    return "\n".join(L)


def find_breaches(agg, cfg, hours):
    """Every threshold trip, with the numbers and the offending brief attached."""
    out = []
    day = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    scale = max(hours / 24.0, 1.0)  # a longer window gets a proportionally higher bar
    cap = cfg["agent_turn_cap"]
    sess_cap = cfg["session_tokens_per_24h"] * scale
    fable_cap = cfg["fable_subagent_tokens_per_24h"] * scale

    over_cap = [(k, b) for k, b in sorted(agg["by_agent"].items(),
                                          key=lambda kv: -kv[1]["tool_turns"])
                if agg["agent_info"].get(k, {}).get("is_subagent") and b["tool_turns"] >= cap]
    max_cards = int(cfg.get("max_agent_cards", 5))
    for akey, b in over_cap[:max_cards]:
        info = agg["agent_info"].get(akey, {})
        desc, where = agent_label(info)
        aid = info.get("agent_id") or akey.split("::")[-1]
        out.append({
            "project": cfg["project"], "severity": "error",
            # An agent id is unique and a finished agent's turn count never changes
            # again, so day-scoping the key re-filed the same completed finding on
            # every later day it was observed: three identical cards for one agent
            # (2026-09-11 walk). Rollups below stay day-scoped; they really are daily.
            "key": f"burn:agent:{aid}",
            "title": f"burn: agent ran {b['tool_turns']:,} tool turns (cap {cap}) — {human(total(b))} tokens",
            "detail": (
                f"Agent {aid} ran {b['tool_turns']:,} tool turns / {b['turns']:,} assistant turns "
                f"against a cap of {cap}, burning {human(total(b))} tokens for "
                f"{b['output']:,} tokens of actual output.\n"
                f"- brief: {desc}\n"
                f"- spawned via: {where}\n"
                f"- model(s): {', '.join(info.get('models', [])) or '?'} "
                f"(meta model={info.get('model_meta')})\n"
                f"- session: {info.get('session')}   profile: {info.get('profile')}\n"
                f"- transcript: {info.get('rel')}\n"
                f"- input {b['input']:,} / output {b['output']:,} / cache-write {b['cache_write']:,} "
                f"/ cache-read {b['cache_read']:,}\n\n"
                "An agent past the cap is almost always re-reading its own grown transcript, "
                "not doing new work. Stop it and re-spawn with a short fresh brief.\n"
                "Filed by ~/.claude/bin/burn-meter.py (rides inside fleet-sentinel)."),
        })

    if len(over_cap) > max_cards:
        rest = over_cap[max_cards:]
        lines = []
        for akey, b in rest[:60]:
            info = agg["agent_info"].get(akey, {})
            desc, where = agent_label(info)
            lines.append(f"- {b['tool_turns']:>5,} tool turns  {human(total(b)):>9}  "
                         f"[{where}]  {desc[:70]}")
        more = f"\n... and {len(rest)-60} more" if len(rest) > 60 else ""
        out.append({
            "project": cfg["project"], "severity": "warn",
            "key": f"burn:agent-rollup:{day}",
            "title": f"burn: {len(rest)} more agents past the {cap}-tool-turn cap "
                     f"(top {max_cards} filed separately)",
            "detail": (
                f"{len(over_cap)} subagents ran past the {cap}-tool-turn cap in the last "
                f"{hours:.1f}h. The {max_cards} worst have their own cards; the rest are "
                "rolled up here so the board does not drown.\n\n"
                + "\n".join(lines) + more + "\n\n"
                "Fix is upstream, not per-agent: every brief carries the stop-at-the-cap "
                "clause and the coordinator re-spawns with a short fresh brief.\n"
                "Filed by ~/.claude/bin/burn-meter.py (rides inside fleet-sentinel)."),
        })

    for (prof, sid), b in sorted(agg["by_session"].items(), key=lambda kv: -total(kv[1])):
        t = total(b)
        if t < sess_cap:
            continue
        lab = agg["session_label"].get((prof, sid), "(subagent-only / unlabeled)")
        out.append({
            "project": cfg["project"], "severity": "error",
            "key": f"burn:session:{sid}",
            "title": f"burn: session {sid[:8]} at {human(t)} tokens (cap {human(sess_cap)}/{hours:.0f}h)",
            "detail": (
                f"Session {sid} ({prof}) burned {human(t)} tokens across {b['turns']:,} "
                f"assistant turns in the last {hours:.1f}h, past the {human(sess_cap)} cap.\n"
                f"- first prompt: {lab}\n"
                f"- input {b['input']:,} / output {b['output']:,} / cache-write {b['cache_write']:,} "
                f"/ cache-read {b['cache_read']:,}\n"
                f"- cache-read share: {100*b['cache_read']/t if t else 0:.1f}%\n\n"
                "A session this size is paying the cache-read tax on an ever-growing thread. "
                "Write a handoff, close it, reopen fresh.\n"
                "Filed by ~/.claude/bin/burn-meter.py (rides inside fleet-sentinel)."),
        })

    fsub = total(agg["fable_main_vs_sub"].get("subagent", zero()))
    if fsub >= fable_cap:
        lines = []
        for akey, b in sorted(((k, agg["by_agent"][k]) for k in agg["fable_agents"]),
                              key=lambda kv: -total(kv[1])):
            info = agg["fable_agents"][akey]
            desc, where = agent_label(info)
            lines.append(f"- {human(total(b))}  [{where}]  {desc}")
        out.append({
            "project": cfg["project"], "severity": "error",
            "key": f"burn:fable-subagents:{day}",
            "title": f"burn: {human(fsub)} Fable tokens in subagents (cap {human(fable_cap)}) — RULE #1",
            "detail": (
                f"Fable ran inside subagents for {human(fsub)} tokens in the last {hours:.1f}h, "
                f"past the {human(fable_cap)} cap. RULE #1: subagents inherit the parent model, "
                "so every Agent call and every workflow arm needs an explicit model=.\n\n"
                "Offending spawns:\n" + "\n".join(lines) + "\n\n"
                "Filed by ~/.claude/bin/burn-meter.py (rides inside fleet-sentinel)."),
        })
    return out


def file_cards(breaches, dry_run):
    filed = 0
    for br in breaches:
        if dry_run:
            print(f"  DRY-RUN would file [{br['project']}] {br['title']} "
                  f"(key={br['key']} sev={br['severity']})")
            filed += 1
            continue
        try:
            subprocess.run([FAILTASK, br["project"], br["title"], "--detail", br["detail"],
                            "--dedupe-key", br["key"], "--severity", br["severity"]],
                           timeout=180, check=False)
            filed += 1
        except Exception as e:
            sys.stderr.write(f"burn-meter: failtask failed for {br['key']}: {e!r}\n")
    return filed


def prune_reports(keep):
    try:
        files = sorted(f for f in os.listdir(BURN_DIR) if f.startswith("burn-") and f.endswith(".txt"))
    except OSError:
        return
    for f in files[:-keep] if len(files) > keep else []:
        try:
            os.remove(os.path.join(BURN_DIR, f))
        except OSError:
            pass


def main():
    args = sys.argv[1:]
    cfg = load_config()
    dry_run = "--dry-run" in args
    quiet = "--quiet" in args
    no_file = "--no-file" in args
    hours = float(cfg["window_hours"])
    since = None
    for i, a in enumerate(args):
        if a == "--hours" and i + 1 < len(args):
            hours = float(args[i + 1])
        if a == "--since" and i + 1 < len(args):
            since = args[i + 1]

    now_local = datetime.now(LOCAL_TZ)
    if since:
        cutoff_local = datetime.fromisoformat(since)
        if cutoff_local.tzinfo is None:
            cutoff_local = cutoff_local.replace(tzinfo=LOCAL_TZ)
        hours = (now_local - cutoff_local).total_seconds() / 3600
    else:
        cutoff_local = now_local - timedelta(hours=hours)
    cutoff_utc = cutoff_local.astimezone(timezone.utc)

    agg = collect(cutoff_utc, cutoff_local.timestamp())
    breaches = find_breaches(agg, cfg, hours)
    report = render(agg, cfg, cutoff_local, now_local, breaches)

    grand = 0
    for b in agg["by_model"].values():
        grand += total(b)
    turns = sum(b["turns"] for b in agg["by_model"].values())

    if not quiet:
        print(report)

    os.makedirs(BURN_DIR, exist_ok=True)
    report_path = os.path.join(BURN_DIR, f"burn-{now_local:%Y-%m-%d}.txt")
    if not dry_run:
        try:
            with open(report_path, "w") as f:
                f.write(report + "\n")
            prune_reports(int(cfg["report_keep"]))
        except OSError as e:
            sys.stderr.write(f"burn-meter: could not write {report_path}: {e}\n")

    filed = file_cards(breaches, dry_run) if (breaches and not no_file) else 0

    # The estate line: what fleet-sentinel and the board see.
    where = "no report written (dry-run)" if dry_run else f"report {report_path}"
    print(f"burn-meter: {human(grand)} tokens / {turns:,} turns over {hours:.0f}h — "
          f"{len(breaches)} threshold breach(es), {filed} card(s) "
          f"{'(dry-run)' if dry_run else 'filed'} — {where}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
