#!/usr/bin/env python3
"""Offline tests for failtask's T-1742 Issue/DraftIssue split (lane L1).

failtask is a bash script wrapping an embedded python3 heredoc, not an
importable module, so this harness extracts the heredoc body, strips the
`main()` auto-invocation at the end, and exec()s the rest into a namespace
with `subprocess` replaced by an in-process stub. Every gh/board-ref call
in the extracted source funnels through that stub, so no real subprocess
ever runs and no network call is made.

Run: python3 ~/.claude/bin/tests/test_failtask_issue_path.py
"""
import json
import os
import re
import sys
import types

# Both layouts (repo: local-tools/failtask + local-tools/tests/; installed:
# ~/.claude/bin/failtask + ~/.claude/bin/tests/) put the tool one directory
# above this test file, so resolving relative to __file__ is right in both;
# FAILTASK_PATH overrides for anything else.
FAILTASK_PATH = os.environ.get("FAILTASK_PATH") or os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "failtask"))
GH = "FAKE_GH"


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def extract_embedded_python():
    src = open(FAILTASK_PATH).read()
    m = re.search(r"<<'PYEOF'\n(.*)\nPYEOF\n", src, re.S)
    assert m, "could not find the python heredoc in failtask"
    body = m.group(1)
    marker = "\ntry:\n    main()"
    idx = body.index(marker)
    return body[:idx]  # drop the auto-invoked main()/sys.exit(0) tail


EMBEDDED_SRC = extract_embedded_python()


def load_module(dry_run=False, board_repo=None, dispatch=None):
    """Exec the embedded source with a stubbed subprocess. Returns
    (namespace, calls) where calls is the list of every command list passed
    to subprocess.run, in order."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if dispatch is not None:
            r = dispatch(cmd)
            if r is not None:
                return r
        joined = " ".join(cmd)
        if "issue create" in joined:
            return FakeResult(0, stdout="https://github.com/bigbrownjeff/board/issues/99\n")
        if "item-add" in joined:
            return FakeResult(0, stdout=json.dumps({"id": "PVTI_new123"}))
        if "item-create" in joined:
            return FakeResult(0, stdout=json.dumps({"id": "PVTI_draftnew"}))
        return FakeResult(0, stdout="")

    fake_subprocess = types.ModuleType("subprocess")
    fake_subprocess.run = fake_run
    real_subprocess = sys.modules.get("subprocess")

    old_dry = os.environ.get("FAILTASK_DRY_RUN")
    old_repo = os.environ.get("FAILTASK_BOARD_REPO")
    if dry_run:
        os.environ["FAILTASK_DRY_RUN"] = "1"
    elif "FAILTASK_DRY_RUN" in os.environ:
        del os.environ["FAILTASK_DRY_RUN"]
    if board_repo is not None:
        os.environ["FAILTASK_BOARD_REPO"] = board_repo
    elif "FAILTASK_BOARD_REPO" in os.environ:
        del os.environ["FAILTASK_BOARD_REPO"]

    sys.modules["subprocess"] = fake_subprocess
    ns = {}
    try:
        exec(compile(EMBEDDED_SRC, "failtask_embedded", "exec"), ns)
    finally:
        if real_subprocess is not None:
            sys.modules["subprocess"] = real_subprocess
        else:
            del sys.modules["subprocess"]
        if old_dry is None:
            os.environ.pop("FAILTASK_DRY_RUN", None)
        else:
            os.environ["FAILTASK_DRY_RUN"] = old_dry
        if old_repo is None:
            os.environ.pop("FAILTASK_BOARD_REPO", None)
        else:
            os.environ["FAILTASK_BOARD_REPO"] = old_repo
    return ns, calls


PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("ok   - %s" % name)
    else:
        FAIL += 1
        print("FAIL - %s%s" % (name, (": " + detail) if detail else ""))


# ---------------------------------------------------------------- (a)
def test_a_reopen_issue_posts_comment_not_body_edit():
    ns, calls = load_module()
    gh_item = {
        "id": "PVTI_x1",
        "status": "Done",
        "content": {
            "type": "Issue", "id": "I_kwDOtest01", "number": 42,
            "url": "https://github.com/bigbrownjeff/board/issues/42",
            "body": "orig body\nfailkey: abc123", "title": "[infra] X",
        },
    }
    rec = {"ts": "2026-08-29T12:00:00-04:00"}
    ok = ns["reopen"](GH, gh_item, rec)
    add_comment_calls = [c for c in calls if "graphql" in c and "addComment" in " ".join(c)]
    body_edit_calls = [c for c in calls if "item-edit" in c and "--body" in c]
    check("a: reopen(Issue) returns True", ok is True)
    check("a: exactly one addComment call", len(add_comment_calls) == 1, str(calls))
    check("a: zero item-edit --body calls", len(body_edit_calls) == 0, str(calls))
    check("a: status flip still requested", any("item-edit" in c and "--single-select-option-id" in c for c in calls))
    check("a: gh_item.status flips to Todo", gh_item["status"] == "Todo")


# ---------------------------------------------------------------- (b)
def test_b_reopen_draft_unchanged():
    ns, calls = load_module()
    gh_item = {
        "id": "PVTI_x2",
        "status": "Done",
        "content": {
            "type": "DraftIssue", "id": "DI_test02",
            "body": "orig body\nfailkey: def456", "title": "[infra] Y",
        },
    }
    rec = {"ts": "2026-08-29T13:00:00-04:00"}
    ok = ns["reopen"](GH, gh_item, rec)
    edit_calls = [c for c in calls if "item-edit" in c and "--id" in c and "DI_test02" in c]
    comment_calls = [c for c in calls if "addComment" in " ".join(c)]
    check("b: reopen(DraftIssue) returns True", ok is True)
    check("b: exactly one draft item-edit --body call", len(edit_calls) == 1, str(calls))
    check("b: zero addComment calls", len(comment_calls) == 0, str(calls))
    check("b: body carries new stamp", gh_item["content"]["body"].endswith("recurred: 2026-08-29T13:00:00-04:00"))
    check("b: title preserved on the edit", any("--title" in c and "[infra] Y" in c for c in edit_calls))


# ---------------------------------------------------------------- (c)
def test_c_create_issue_then_item_add():
    ns, calls = load_module()
    item_id, ctype = ns["create_issue_and_add"](GH, "[infra] Z: title", "body text")
    check("c: content_type is Issue", ctype == "Issue")
    check("c: item id parsed from item-add JSON", item_id == "PVTI_new123")
    idx_create = next((i for i, c in enumerate(calls) if "issue" in c and "create" in c), None)
    idx_add = next((i for i, c in enumerate(calls) if "item-add" in c), None)
    check("c: both calls happened", idx_create is not None and idx_add is not None, str(calls))
    check("c: issue create happens before item-add", idx_create is not None and idx_add is not None and idx_create < idx_add)
    check("c: issue create targets the configured board repo", any("bigbrownjeff/board" in c for c in calls[:1]), str(calls[:1]))


# ---------------------------------------------------------------- (c-fallback, review fix 1)
def test_c2_create_falls_back_when_repo_absent():
    def dispatch(cmd):
        if "issue" in cmd and "create" in cmd:
            return FakeResult(1, stdout="", stderr="GraphQL: Could not resolve to a Repository (repo)")
        return None
    ns, calls = load_module(dispatch=dispatch)
    item_id, ctype = ns["create_issue_and_add"](GH, "[infra] Z: title", "body text")
    check("c2: falls back to DraftIssue when repo is absent", ctype == "DraftIssue")
    check("c2: fallback item id parsed", item_id == "PVTI_draftnew")
    check("c2: item-create fallback was actually called", any("item-create" in c for c in calls), str(calls))


# ---------------------------------------------------------------- (d)
def test_d_find_match_scans_issue_body():
    ns, _ = load_module()
    items = [
        {"id": "PVTI_open1", "status": "Todo",
         "content": {"type": "Issue", "id": "I_a", "body": "context\nfailkey: xyz789"}},
        {"id": "PVTI_other", "status": "Done",
         "content": {"type": "Issue", "id": "I_b", "body": "unrelated"}},
    ]
    open_hit, done_hit = ns["find_match"](items, "failkey: xyz789")
    check("d: matches an open Issue-shaped body", open_hit is not None and open_hit["id"] == "PVTI_open1")
    check("d: no Done match for a different key", done_hit is None)


# ---------------------------------------------------------------- (e)
def test_e_dry_run_makes_zero_calls():
    ns, calls = load_module(dry_run=True)
    check("e: DRYRUN constant is True", ns["DRYRUN"] is True)

    gh_issue = {"id": "PVTI_e1", "status": "Done",
                "content": {"type": "Issue", "id": "I_e1", "number": 7,
                            "url": "https://github.com/bigbrownjeff/board/issues/7",
                            "body": "b", "title": "t"}}
    ns["reopen"](GH, gh_issue, {"ts": "2026-08-29T00:00:00Z"})
    check("e: reopen(Issue) makes zero real calls under DRYRUN", len(calls) == 0, str(calls))

    gh_draft = {"id": "PVTI_e2", "status": "Done",
                "content": {"type": "DraftIssue", "id": "DI_e2", "body": "b", "title": "t"}}
    ns["reopen"](GH, gh_draft, {"ts": "2026-08-29T00:00:00Z"})
    check("e: reopen(DraftIssue) makes zero real calls under DRYRUN", len(calls) == 0, str(calls))

    ns["create_issue_and_add"](GH, "title", "body")
    check("e: create_issue_and_add makes zero real calls under DRYRUN", len(calls) == 0, str(calls))


# --------------------------------------------- (f) E.1 gate: dupe path is silent
def test_f_open_match_makes_no_board_write():
    """The 'SEEN AGAIN' path fires on every tick of a still-broken thing.
    It records the recurrence in failures.jsonl and writes nothing to the
    board, on either content type: one comment per tick would be a
    notification storm and mutation quota spent against the same secondary
    limit the 2026-08-27 trip hit."""
    for ctype, cid in (("Issue", "I_open"), ("DraftIssue", "DI_open")):
        ns, calls = load_module()
        items = [{"id": "PVTI_open", "status": "Todo",
                  "content": {"type": ctype, "id": cid, "number": 8,
                              "url": "https://github.com/bigbrownjeff/board/issues/8",
                              "body": "ctx\nfailkey: rep123"}}]
        rec = {"ts": "2026-08-29T13:00:00-04:00", "key": "rep123", "host": "h",
               "severity": "warn", "project": "infra", "title": "t", "detail": ""}
        outcome, item_id = ns["board_create"](GH, rec, items, None)
        check("f: %s open match is a dupe" % ctype, outcome == "dupe", outcome)
        check("f: %s open match writes nothing to the board" % ctype,
              len(calls) == 0, str(calls))
    check("f: note_recurrence is gone entirely", "note_recurrence" not in ns, "")


# ------------------------------- (g) E.1 gate: a created issue is never re-filed
def test_g_no_double_file_after_a_successful_create():
    """Once `gh issue create` succeeds the issue exists. A failure parsing
    item-add's payload must NOT fall through to item-create: that would file
    the same card twice, once as an issue and once as a draft."""
    def dispatch(cmd):
        if "item-add" in cmd:
            return FakeResult(0, stdout="not json at all")
        return None
    ns, calls = load_module(dispatch=dispatch)
    item_id, ctype = ns["create_issue_and_add"](GH, "[infra] Z", "body")
    check("g: returns failure rather than a draft", (item_id, ctype) == (None, None),
          str((item_id, ctype)))
    check("g: item-create was never called", not any("item-create" in c for c in calls),
          str(calls))

    def dispatch_empty(cmd):
        if "issue" in cmd and "create" in cmd:
            return FakeResult(0, stdout="")   # created, but printed nothing
        return None
    ns, calls = load_module(dispatch=dispatch_empty)
    item_id, ctype = ns["create_issue_and_add"](GH, "[infra] Z", "body")
    check("g: an empty create stdout does not double-file either",
          (item_id, ctype) == (None, None) and not any("item-create" in c for c in calls),
          str(calls))


# ------------------------------------------------- (h) P2-13: labels on create
def test_h_create_issue_carries_failure_and_claude_labels():
    """Every failtask card sets Owner=agent unconditionally (board_create),
    so every one also gets the `claude` label alongside `failure` (gate
    review P2-13: issue #1 landed unlabelled)."""
    ns, calls = load_module()
    ns["create_issue_and_add"](GH, "[infra] Z: title", "body text")
    create_call = next(c for c in calls if "issue" in c and "create" in c)
    check("h: --label failure present", "failure" in create_call, str(create_call))
    check("h: --label claude present", "claude" in create_call, str(create_call))
    check("h: labels use ISSUE_LABELS constant", ns["ISSUE_LABELS"] == ["failure", "claude"])


# ------------------------------------------- (i) P2-13: label-only failure retries
def test_i_label_only_failure_retries_without_labels():
    """A repo missing the `failure`/`claude` labels (e.g. run before C.2's
    label setup) must not fail the whole create; retry once without labels."""
    state = {"n": 0}

    def dispatch(cmd):
        if "issue" in cmd and "create" in cmd:
            state["n"] += 1
            if "--label" in cmd:
                return FakeResult(1, stdout="", stderr="could not add label: 'failure' not found")
            return FakeResult(0, stdout="https://github.com/bigbrownjeff/board/issues/100\n")
        return None
    ns, calls = load_module(dispatch=dispatch)
    item_id, ctype = ns["create_issue_and_add"](GH, "[infra] Z", "body")
    create_calls = [c for c in calls if "issue" in c and "create" in c]
    check("i: two create attempts (labeled, then bare)", state["n"] == 2, str(create_calls))
    check("i: first attempt carries --label", "--label" in create_calls[0], str(create_calls))
    check("i: second attempt carries no --label", "--label" not in create_calls[1], str(create_calls))
    check("i: still resolves to Issue, not a draft fallback", ctype == "Issue", str((item_id, ctype)))
    check("i: item-create fallback never called", not any("item-create" in c for c in calls), str(calls))


if __name__ == "__main__":
    test_a_reopen_issue_posts_comment_not_body_edit()
    test_b_reopen_draft_unchanged()
    test_c_create_issue_then_item_add()
    test_c2_create_falls_back_when_repo_absent()
    test_d_find_match_scans_issue_body()
    test_e_dry_run_makes_zero_calls()
    test_f_open_match_makes_no_board_write()
    test_g_no_double_file_after_a_successful_create()
    test_h_create_issue_carries_failure_and_claude_labels()
    test_i_label_only_failure_retries_without_labels()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
