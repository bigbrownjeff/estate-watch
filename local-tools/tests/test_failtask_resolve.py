#!/usr/bin/env python3
"""Offline tests for `failtask --resolve`.

Same harness as test_failtask_issue_path.py: the embedded python is extracted and
exec()d with `subprocess` replaced by an in-process stub, so no real gh, board or
network is ever touched. FAILDIR/CACHE/LOCK_PATH are redirected to a temp dir.
Cases (h) and the mutant run the real script/test in a child process; (h) exits
before any gh call, the mutant child still uses the stub.

Mutant proof: a copy of failtask has the whole-line equality in find_open_all
weakened to a substring test; case (b) must go red against it.

Run: python3 ~/.claude/bin/tests/test_failtask_resolve.py
"""
import json
import os
import re
import subprocess as real_subprocess
import sys
import tempfile
import time
import types

FAILTASK_PATH = os.environ.get("FAILTASK_PATH") or os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "failtask"))
SELF = os.path.abspath(__file__)
GH = "FAKE_GH"
OPT_DONE = "DONE_OPT"
EQ_LINE = 'if not any(l.strip() == marker for l in body.splitlines()):'
SUB_LINE = 'if marker not in body:'


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def embedded_src():
    src = open(FAILTASK_PATH).read()
    m = re.search(r"<<'PYEOF'\n(.*)\nPYEOF\n", src, re.S)
    assert m, "could not find the python heredoc in failtask"
    body = m.group(1)
    return body[:body.index("\ntry:\n    main()")]


SRC = embedded_src()


def card(cid, key, project="infra", status="Todo", ctype="Issue", number=7):
    return {"id": cid, "status": status,
            "content": {"type": ctype, "id": "I_" + cid, "number": number,
                        "url": "https://github.com/bigbrownjeff/board/issues/%d" % number,
                        "title": "[FAILURE] " + cid,
                        "body": "detail\nfailkey: %s\nproject-raw: %s\n" % (key, project)}}


def is_write(cmd):
    j = " ".join(cmd)
    return "addComment" in j or "PATCH" in j or "item-edit" in j


def kind(cmd):
    j = " ".join(cmd)
    if "addComment" in j: return "comment"
    if "PATCH" in j: return "close"
    if "item-edit" in j and FAKE_STATUS_FIELD in j: return "status"
    return "other"


FAKE_STATUS_FIELD = "PVTSSF_lAHOARM7z84Bd1kfzhYULaw"


def run_resolve(items, project="infra", key="foo", live_status="Todo", fail=None,
                dry=False, note="healthy"):
    """Returns (rc, stdout, stderr, calls). `fail` = kind that returns rc 1."""
    tmp = tempfile.mkdtemp(prefix="ft-resolve-")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        j = " ".join(cmd)
        if "fieldValueByName" in j:
            return FakeResult(0, json.dumps({"data": {"node": {
                "fieldValueByName": {"name": live_status},
                "content": {"__typename": "Issue", "id": "LIVE_ID", "number": 7,
                            "url": "https://github.com/bigbrownjeff/board/issues/7"}}}}))
        if "ProjectV2SingleSelectField" in j:
            return FakeResult(0, json.dumps({"data": {"node": {"options": [
                {"id": "x", "name": "Todo"}, {"id": OPT_DONE, "name": "Done"}]}}}))
        if fail and kind(cmd) == fail:
            return FakeResult(1, "", "boom")
        return FakeResult(0, "{}")

    stub = types.ModuleType("subprocess")
    stub.run = fake_run
    saved = sys.modules.get("subprocess")
    sys.modules["subprocess"] = stub
    ns = {}
    try:
        exec(compile(SRC, "failtask_embedded", "exec"), ns)
    finally:
        sys.modules["subprocess"] = saved
    ns.update(FAILDIR=tmp, CACHE=os.path.join(tmp, "board-cache.json"),
              LOCK_PATH=os.path.join(tmp, ".lock"), find_gh=lambda: GH)
    with open(ns["CACHE"], "w") as f:
        json.dump({"v": ns["CACHE_VERSION"], "items": items}, f)
    env = {"FT_PROJECT": project, "FT_KEY": key, "FT_NOTE": note,
           "FT_RESOLVE_DRY": "1" if dry else "0"}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    out, err = [], []
    so, se = sys.stdout, sys.stderr
    sys.stdout = types.SimpleNamespace(write=out.append, flush=lambda: None)
    sys.stderr = types.SimpleNamespace(write=err.append, flush=lambda: None)
    try:
        rc = ns["resolve_main"]()
    finally:
        sys.stdout, sys.stderr = so, se
        for k, v in old.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v
    return rc, "".join(out), "".join(err), calls


PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print("ok   - %s" % name)
    else:
        FAIL += 1; print("FAIL - %s%s" % (name, (": " + detail) if detail else ""))


def writes(calls):
    return [c for c in calls if is_write(c)]


def test_a_exact_key_closes_one_card_in_order():
    rc, out, err, calls = run_resolve([card("C1", "foo"), card("C2", "bar")])
    ks = [kind(c) for c in writes(calls)]
    check("a: exit 0", rc == 0, str(rc))
    check("a: comment, close, status in that order", ks == ["comment", "close", "status"], str(ks))
    st = writes(calls)[-1]
    check("a: Status write is last, targets C1 with the Done option",
          "C1" in st and OPT_DONE in st, str(st))
    check("a: close PATCHes issue 7", "repos/bigbrownjeff/board/issues/7" in " ".join(writes(calls)[1]))
    check("a: says RESOLVED", "RESOLVED infra/foo" in out, out)


def test_b_prefix_or_substring_key_closes_nothing():
    for label, stored, asked in (("prefix", "foo-bar", "foo"), ("substring", "xfoox", "foo"),
                                 ("shorter-asked", "foo", "fo")):
        rc, out, err, calls = run_resolve([card("C1", stored)], key=asked)
        check("b: %s (%s vs %s) exits 0" % (label, asked, stored), rc == 0, str(rc))
        check("b: %s says nothing open" % label, "nothing open" in out, out)
        check("b: %s issues no write" % label, not writes(calls), str(calls))


def test_c_project_raw_mismatch_closes_nothing():
    rc, out, err, calls = run_resolve([card("C1", "foo", project="other")], project="infra")
    check("c: exit 0, nothing open", rc == 0 and "nothing open" in out, "%s %s" % (rc, out))
    check("c: no write", not writes(calls), str(calls))
    rc, out, err, calls = run_resolve([card("C1", "foo", project="infra-extra")], project="infra")
    check("c: project prefix also closes nothing", "nothing open" in out and not writes(calls), out)


def test_d_two_open_matches_exit_2_no_write():
    rc, out, err, calls = run_resolve([card("C1", "foo", number=7), card("C2", "foo", number=8)])
    check("d: exit 2", rc == 2, str(rc))
    check("d: both ids listed", "C1" in err and "C2" in err, err)
    check("d: no write call", not writes(calls), str(writes(calls)))


def test_e_already_done_exits_0_writes_nothing():
    rc, out, err, calls = run_resolve([card("C1", "foo", status="Done")])
    check("e: cached Done: exit 0, nothing open", rc == 0 and "nothing open" in out, out)
    check("e: cached Done: no write", not writes(calls))
    rc, out, err, calls = run_resolve([card("C1", "foo")], live_status="Done")
    check("e: live Done (cache stale): exit 0, nothing open", rc == 0 and "nothing open" in out, out)
    check("e: live Done: no write", not writes(calls), str(writes(calls)))


def test_f_dry_run_no_write_prints_plan():
    rc, out, err, calls = run_resolve([card("C1", "foo")], dry=True)
    check("f: exit 0", rc == 0, str(rc))
    check("f: no real write call", not writes(calls), str(writes(calls)))
    check("f: prints the plan", "DRY-RUN plan for infra/foo" in out and "set Status -> Done" in out, out)
    check("f: plan routed through gh_write", err.count("DRY-RUN would run: gh") == 3, err)


def test_g_close_error_exits_1_status_not_set():
    rc, out, err, calls = run_resolve([card("C1", "foo")], fail="close")
    ks = [kind(c) for c in writes(calls)]
    check("g: exit 1", rc == 1, str(rc))
    check("g: comment and close attempted", ks == ["comment", "close"], str(ks))
    check("g: Status never set to Done", "status" not in ks and not any(OPT_DONE in c for c in calls
          if "item-edit" in " ".join(c)), str(calls))


def test_h_missing_project_or_key_exits_2():
    for argv in (["--resolve"], ["--resolve", "infra"], ["--resolve", "infra", "--note", "x"]):
        r = real_subprocess.run(["/bin/bash", FAILTASK_PATH] + argv, capture_output=True, text=True,
                                timeout=30, env={"PATH": "/nonexistent", "HOME": tempfile.mkdtemp()})
        check("h: %s exits 2" % " ".join(argv), r.returncode == 2, "%s %s" % (r.returncode, r.stderr))


def test_z_mutant_substring_match_turns_b_red():
    src = open(FAILTASK_PATH).read()
    assert src.count(EQ_LINE) == 1, "whole-line equality line not found exactly once; update the mutant"
    d = tempfile.mkdtemp(prefix="ft-mutant-")
    mpath = os.path.join(d, "failtask")
    open(mpath, "w").write(src.replace(EQ_LINE, SUB_LINE))
    env = dict(os.environ, FAILTASK_PATH=mpath, FAILTASK_ONLY_B="1")
    r = real_subprocess.run([sys.executable, SELF], capture_output=True, text=True, env=env, timeout=60)
    check("z: mutant (substring test) makes case b go red", r.returncode != 0 and "FAIL - b:" in r.stdout,
          r.stdout[-400:])
    env["FAILTASK_PATH"] = FAILTASK_PATH
    r = real_subprocess.run([sys.executable, SELF], capture_output=True, text=True, env=dict(env), timeout=60)
    check("z: same run against the real tool is green", r.returncode == 0, r.stdout[-400:])


if __name__ == "__main__":
    test_b_prefix_or_substring_key_closes_nothing()
    if not os.environ.get("FAILTASK_ONLY_B"):
        test_a_exact_key_closes_one_card_in_order()
        test_c_project_raw_mismatch_closes_nothing()
        test_d_two_open_matches_exit_2_no_write()
        test_e_already_done_exits_0_writes_nothing()
        test_f_dry_run_no_write_prints_plan()
        test_g_close_error_exits_1_status_not_set()
        test_h_missing_project_or_key_exits_2()
        if "ft-mutant-" not in FAILTASK_PATH and not os.environ.get("FAILTASK_NO_MUTANT"):
            test_z_mutant_substring_match_turns_b_red()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
