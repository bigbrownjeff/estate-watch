#!/usr/bin/env python3
"""phoenix-enrich — make Claude Code's OTel spans legible to Phoenix.

THE PROBLEM (diagnosed 2026-08-28)
Claude Code exports token counts as flat span attributes:
    input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens
Phoenix follows the OpenInference semantic convention and reads:
    llm.token_count.prompt, llm.token_count.completion, llm.token_count.total
Nothing maps one to the other, so Phoenix's llm_token_count_prompt /
llm_token_count_completion columns are 0 on every span and the UI reports zero
tokens for everything. The data was always arriving; it was landing under names
Phoenix does not look at. Verified by sending a probe span with
llm.token_count.prompt=111 — the column populated immediately.

Phoenix does NOT upsert a span that is re-sent with the same span_id (verified:
the second export was dropped), so re-pushing corrected spans over OTLP cannot
fix spans already ingested. This normalizes them in place instead.

WHAT IT WRITES, per claude_code.llm_request span:
  llm.token_count.{prompt,completion,total}
  llm.token_count.prompt_details.{cache_read,cache_write,input}
  the indexed llm_token_count_prompt / llm_token_count_completion columns
  cumulative_llm_token_count_* (rolled up through each trace's parent chain)
  llm.cost.{prompt,completion,total}   API list-price estimate, NOT plan usage
And, on every span carrying an agent_id, joined read-only from the transcripts:
  persona (attributionAgent / agentType), agent.brief, agent.model_requested,
  agent.spawn_depth, agent.parent_id, profile
Claude Code already emits model, tool_name, duration_ms, session.id, agent_id,
workflow.name and workflow.run_id, so those are left alone.

Read-only on transcripts (it reads *.meta.json sidecars only). Writes only to
Phoenix's own SQLite, which agent-lab documents as disposable.

Usage:
  phoenix-enrich.py [--hours N] [--all] [--db PATH] [--dry-run] [--quiet]
"""
import glob
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

HOME = os.path.expanduser("~")
DB_PATH = os.path.join(HOME, "Projects", "agent-lab", "phoenix", "data", "phoenix.db")
PROFILES = {
    "claude (jeffpinto.com)": os.path.join(HOME, ".claude", "projects"),
    "claude-claudine (gmail)": os.path.join(HOME, ".claude-claudine", "projects"),
    # Four sister profiles since 2026-09-02 (Jeff, 09-07: "treated in parallel").
    # claudette ran the busiest sessions of the week and was invisible here
    # until the 2026-09-09 walk; the turn-cap watchdog must see every profile.
    "claude-claudette (jeffpinto.com)": os.path.join(HOME, ".claude-claudette", "projects"),
    "claude-claudeux (bluecamel)": os.path.join(HOME, ".claude-claudeux", "projects"),
}

# Anthropic API list prices, $ per 1M tokens (claude-api skill, cached 2026-06-24).
# Cache write = 1.25x input, cache read = 0.1x input (standard 5-minute-TTL
# multipliers). This is a LIST-PRICE estimate for relative comparison between
# lanes; it is NOT what a Max subscription meters against its limit.
PRICES = {  # model prefix -> (input, output)
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10


def price_for(model):
    if not model:
        return None
    base = model.split("[")[0]  # claude-opus-5[1m] -> claude-opus-5
    if base in PRICES:
        return PRICES[base]
    for k, v in sorted(PRICES.items(), key=lambda kv: -len(kv[0])):
        if base.startswith(k):
            return v
    return None


def build_agent_index():
    """agent_id -> persona / brief / requested model, from *.meta.json sidecars only."""
    index = {}
    for profile, root in PROFILES.items():
        if not os.path.isdir(root):
            continue
        for mp in glob.iglob(os.path.join(root, "*", "*", "subagents", "**", "agent-*.meta.json"),
                             recursive=True):
            m = re.search(r"agent-([0-9a-f]+)\.meta\.json$", mp)
            if not m:
                continue
            try:
                with open(mp) as f:
                    meta = json.load(f)
            except Exception:
                continue
            parts = mp.split(os.sep)
            workflow_id = None
            if "workflows" in parts:
                wi = parts.index("workflows")
                if wi + 1 < len(parts):
                    workflow_id = parts[wi + 1]
            index[m.group(1)] = {
                "persona": meta.get("agentType") or "unknown",
                "brief": (meta.get("description") or "")[:300],
                "model_requested": meta.get("model"),
                "spawn_depth": meta.get("spawnDepth"),
                "parent_id": meta.get("parentAgentId"),
                "workflow_id": workflow_id,
                "profile": profile,
            }
    return index


def nest(attrs, dotted, value):
    """Set a dotted OpenInference key into Phoenix's nested attribute JSON."""
    parts = dotted.split(".")
    node = attrs
    for p in parts[:-1]:
        nxt = node.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            node[p] = nxt
        node = nxt
    node[parts[-1]] = value


def get(attrs, dotted, default=None):
    node = attrs
    for p in dotted.split("."):
        if not isinstance(node, dict) or p not in node:
            return default
        node = node[p]
    return node


def main():
    args = sys.argv[1:]
    dry = "--dry-run" in args
    quiet = "--quiet" in args
    hours = 48.0
    for i, a in enumerate(args):
        if a == "--hours" and i + 1 < len(args):
            hours = float(args[i + 1])
    do_all = "--all" in args
    db_path = DB_PATH
    for i, a in enumerate(args):
        if a == "--db" and i + 1 < len(args):
            db_path = os.path.expanduser(args[i + 1])

    if not os.path.exists(db_path):
        print(f"phoenix-enrich: no Phoenix DB at {db_path} — is com.jeffpinto.phoenix running?",
              file=sys.stderr)
        return 1

    agents = build_agent_index()
    con = sqlite3.connect(db_path, timeout=60)
    con.execute("PRAGMA busy_timeout=30000")  # Phoenix holds WAL writers; never fail fast

    if do_all:
        where, params = "1=1", ()
    else:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        where, params = "start_time >= ?", (cutoff,)

    rows = con.execute(
        f"SELECT id, trace_rowid, span_id, parent_id, name, attributes, "
        f"llm_token_count_prompt, llm_token_count_completion "
        f"FROM spans WHERE {where}", params).fetchall()

    updates, traces_touched = [], set()
    n_tokens, n_persona, n_cost, n_skipped = 0, 0, 0, 0
    tok_prompt_total, tok_completion_total, cost_total = 0, 0, 0.0

    for sid, trace_rowid, span_id, parent_id, name, attr_json, col_p, col_c in rows:
        try:
            attrs = json.loads(attr_json) if attr_json else {}
        except Exception:
            n_skipped += 1
            continue
        if not isinstance(attrs, dict):
            n_skipped += 1
            continue
        changed = False
        new_p, new_c = col_p, col_c

        inp = attrs.get("input_tokens")
        out = attrs.get("output_tokens")
        if isinstance(inp, int) or isinstance(out, int):
            inp = inp or 0
            out = out or 0
            cr = attrs.get("cache_read_tokens") or 0
            cw = attrs.get("cache_creation_tokens") or 0
            prompt = inp + cr + cw
            completion = out
            if get(attrs, "llm.token_count.prompt") != prompt or col_p != prompt:
                nest(attrs, "llm.token_count.prompt", prompt)
                nest(attrs, "llm.token_count.completion", completion)
                nest(attrs, "llm.token_count.total", prompt + completion)
                nest(attrs, "llm.token_count.prompt_details.cache_read", cr)
                nest(attrs, "llm.token_count.prompt_details.cache_write", cw)
                nest(attrs, "llm.token_count.prompt_details.input", inp)
                new_p, new_c = prompt, completion
                changed = True
                n_tokens += 1
                tok_prompt_total += prompt
                tok_completion_total += completion

            pr = price_for(attrs.get("model") or get(attrs, "llm.model_name"))
            if pr and get(attrs, "llm.cost.total") is None:
                pin, pout = pr
                c_prompt = (inp * pin + cw * pin * CACHE_WRITE_MULT
                            + cr * pin * CACHE_READ_MULT) / 1e6
                c_completion = out * pout / 1e6
                nest(attrs, "llm.cost.prompt", round(c_prompt, 6))
                nest(attrs, "llm.cost.completion", round(c_completion, 6))
                nest(attrs, "llm.cost.total", round(c_prompt + c_completion, 6))
                nest(attrs, "llm.cost.basis", "anthropic api list price, not plan usage")
                changed = True
                n_cost += 1
                cost_total += c_prompt + c_completion

        aid = attrs.get("agent_id")
        if aid and attrs.get("persona") is None:
            info = agents.get(aid)
            if info:
                attrs["persona"] = info["persona"]
                nest(attrs, "agent.brief", info["brief"])
                nest(attrs, "agent.model_requested", info["model_requested"])
                nest(attrs, "agent.spawn_depth", info["spawn_depth"])
                nest(attrs, "agent.parent_id", info["parent_id"])
                attrs["profile"] = info["profile"]
                if info["workflow_id"] and get(attrs, "workflow.run_id") is None:
                    nest(attrs, "workflow.run_id", info["workflow_id"])
            else:
                # An agent_id with no sidecar is a main-loop or in-flight agent,
                # not an error. Label it so the field is never silently absent.
                attrs["persona"] = "main-loop (no persona)"
            changed = True
            n_persona += 1
        elif not aid and attrs.get("persona") is None and get(attrs, "session.id"):
            attrs["persona"] = "main-loop (no persona)"
            changed = True
            n_persona += 1

        if changed:
            updates.append((json.dumps(attrs, separators=(",", ":")), new_p, new_c, sid))
            traces_touched.add(trace_rowid)

    if dry:
        print(f"phoenix-enrich: DRY-RUN — {len(rows)} spans in window, would update {len(updates)} "
              f"({n_tokens} token-count, {n_persona} persona, {n_cost} cost), "
              f"{len(traces_touched)} traces")
        con.close()
        return 0

    with con:
        con.executemany(
            "UPDATE spans SET attributes = ?, llm_token_count_prompt = ?, "
            "llm_token_count_completion = ? WHERE id = ?", updates)

        # Roll the leaf counts up each touched trace so Phoenix's trace-level
        # token totals stop reading zero.
        for tr in traces_touched:
            span_rows = con.execute(
                "SELECT id, span_id, parent_id, COALESCE(llm_token_count_prompt,0), "
                "COALESCE(llm_token_count_completion,0) FROM spans WHERE trace_rowid = ?",
                (tr,)).fetchall()
            own = {r[1]: (r[3], r[4]) for r in span_rows}
            kids = {}
            for r in span_rows:
                kids.setdefault(r[2], []).append(r[1])
            memo = {}

            def subtree(sp, seen=None):
                seen = seen or set()
                if sp in memo:
                    return memo[sp]
                if sp in seen:  # a cycle would otherwise recurse forever
                    return (0, 0)
                seen = seen | {sp}
                p, c = own.get(sp, (0, 0))
                for k in kids.get(sp, []):
                    kp, kc = subtree(k, seen)
                    p += kp
                    c += kc
                memo[sp] = (p, c)
                return memo[sp]

            con.executemany(
                "UPDATE spans SET cumulative_llm_token_count_prompt = ?, "
                "cumulative_llm_token_count_completion = ? WHERE id = ?",
                [(*subtree(r[1]), r[0]) for r in span_rows])
    con.close()

    if not quiet:
        print(f"phoenix-enrich: {len(rows)} spans in window, {len(updates)} updated "
              f"({n_tokens} token-count, {n_persona} persona, {n_cost} cost), "
              f"{len(traces_touched)} traces rolled up, {n_skipped} unparseable")
    print(f"phoenix-enrich: normalized {tok_prompt_total:,} prompt + {tok_completion_total:,} "
          f"completion tokens, ${cost_total:,.2f} list-price estimate "
          f"(NOT plan usage) over the last {'all time' if do_all else f'{hours:.0f}h'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
