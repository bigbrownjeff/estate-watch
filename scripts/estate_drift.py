#!/usr/bin/env python3
"""Read-only estate drift audit driven by config/estate-drift.json.

The checker never writes, installs, fetches, repairs, launches, or deploys. Network
probes are opt-in. Results distinguish public, gated, intentionally unpublished,
and intentionally retired surfaces and record the terminal status, redirect chain,
and verification date.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "dist", "build", ".next", ".astro"}


def default_manifest() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "estate-drift.json"


def expand(value: str) -> str:
    home = str(Path.home())
    projects = os.environ.get("PROJECTS_ROOT", str(Path.home() / "Projects"))
    return os.path.expandvars(value.replace("${HOME}", home).replace("${PROJECTS}", projects))


def result(check: dict[str, Any], state: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"id": check["id"], "type": check["type"], "state": state, "detail": detail, **extra}


def digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_digest(root: str) -> str:
    """Hash a directory by relative path, file content, and symlink target."""
    base = Path(root)
    h = hashlib.sha256()
    for path in sorted(base.rglob("*"), key=lambda item: item.relative_to(base).as_posix()):
        rel_path = path.relative_to(base)
        if (
            path.name == ".DS_Store"
            or "__pycache__" in rel_path.parts
            or path.suffix == ".pyc"
        ):
            continue
        rel = rel_path.as_posix().encode()
        if path.is_symlink():
            h.update(b"L\0" + rel + b"\0" + os.readlink(path).encode() + b"\0")
        elif path.is_file():
            h.update(b"F\0" + rel + b"\0")
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
            h.update(b"\0")
    return h.hexdigest()


def resolve_paths(patterns: list[str]) -> tuple[list[str], list[str]]:
    matched: list[str] = []
    missed: list[str] = []
    for raw in patterns:
        pattern = expand(raw)
        paths = sorted(glob.glob(pattern, recursive=True))
        if not paths and os.path.lexists(pattern):
            paths = [pattern]
        if paths:
            matched.extend(paths)
        else:
            missed.append(pattern)
    return list(dict.fromkeys(matched)), missed


def check_path_exists(check: dict[str, Any]) -> dict[str, Any]:
    path = expand(check["path"])
    kind = check.get("kind")
    okay = os.path.exists(path)
    if kind == "file":
        okay = os.path.isfile(path)
    elif kind == "dir":
        okay = os.path.isdir(path)
    return result(check, "PASS" if okay else "FAIL", f"{kind or 'path'} {'exists' if okay else 'missing'}: {path}")


def check_same_file(check: dict[str, Any]) -> dict[str, Any]:
    source = expand(check["source"])
    target = expand(check["target"])
    missing = [p for p in (source, target) if not os.path.isfile(p)]
    if missing:
        return result(check, "FAIL", "missing: " + ", ".join(missing))
    source_hash = digest(source)
    target_hash = digest(target)
    okay = source_hash == target_hash
    detail = f"source={source} target={target} sha256={source_hash[:12]}"
    if not okay:
        detail += f" target_sha256={target_hash[:12]}"
    return result(check, "PASS" if okay else "FAIL", detail)


def check_git_tracked(check: dict[str, Any]) -> dict[str, Any]:
    """Require canonical authority paths to exist, be tracked, and be clean."""
    repo = Path(expand(check["repo"])).resolve()
    if not repo.is_dir():
        return result(check, "FAIL", f"repository missing: {repo}")
    top = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != repo:
        return result(check, "FAIL", f"not the canonical checkout root: {repo}")
    findings: list[str] = []
    for relative in check.get("paths", []):
        path = repo / relative
        if not path.exists():
            findings.append(f"missing={relative}")
            continue
        tracked = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "--", relative],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if tracked.returncode != 0 or not tracked.stdout.strip():
            findings.append(f"untracked={relative}")
            continue
        dirty = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--", relative],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if dirty.returncode != 0 or dirty.stdout.strip():
            findings.append(f"dirty={relative}")
    if findings:
        return result(check, "FAIL", "; ".join(findings))
    return result(check, "PASS", f"{len(check.get('paths', []))} canonical path(s) tracked and clean in {repo}")


def check_runtime_receipt(check: dict[str, Any]) -> dict[str, Any]:
    """Independently validate release provenance and reproduce the runtime hash."""
    root = Path(expand(check["root"])).resolve()
    receipt = Path(expand(check["receipt"]))
    try:
        receipt_text = receipt.read_text(encoding="ascii")
    except OSError as exc:
        return result(check, "FAIL", f"cannot read runtime receipt: {receipt}: {exc.__class__.__name__}")
    fields: dict[str, str] = {}
    for raw_line in receipt_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            return result(check, "FAIL", f"invalid runtime receipt line: {receipt}")
        key, value = line.split("=", 1)
        if not key or key in fields:
            return result(check, "FAIL", f"invalid or duplicate runtime receipt field: {receipt}")
        fields[key] = value

    required_fields = {
        "schema", "repo", "revision", "branch", "runtime_sha256",
        "launcher_sha256", "python", "test_command", "approved_at",
    }
    missing_fields = sorted(required_fields - fields.keys())
    if missing_fields:
        return result(check, "FAIL", "runtime receipt missing: " + ", ".join(missing_fields))

    expected = fields["runtime_sha256"]
    expected_launcher = fields["launcher_sha256"]
    findings: list[str] = []
    if fields["schema"] != check.get("schema", "outbound-crm-runtime-approval.v1"):
        findings.append("schema mismatch")
    if Path(fields["repo"]).resolve() != root:
        findings.append("repo mismatch")
    if re.fullmatch(r"[0-9a-f]{40}", fields["revision"]) is None:
        findings.append("invalid revision")
    if fields["branch"] != check.get("branch", "main"):
        findings.append("approved branch is not main")
    if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        findings.append("invalid runtime_sha256")
    if re.fullmatch(r"[0-9a-f]{64}", expected_launcher) is None:
        findings.append("invalid launcher_sha256")
    expected_python = expand(check.get("python", ""))
    if not expected_python or os.path.realpath(fields["python"]) != os.path.realpath(expected_python):
        findings.append("python mismatch")
    if fields["test_command"] != check.get("test_command", ""):
        findings.append("test command mismatch")
    try:
        approved_at = dt.datetime.fromisoformat(fields["approved_at"].replace("Z", "+00:00"))
        if approved_at.tzinfo is None:
            raise ValueError("timezone required")
    except ValueError:
        findings.append("invalid approved_at")

    def git_read(*args: str) -> str | None:
        completed = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            timeout=30, check=False,
        )
        if completed.returncode != 0:
            findings.append("git " + " ".join(args) + " failed")
            return None
        return completed.stdout.strip()

    actual_revision = git_read("rev-parse", "HEAD")
    actual_branch = git_read("branch", "--show-current")
    dirty = git_read("status", "--porcelain", "--untracked-files=all")
    if actual_revision is not None and actual_revision != fields["revision"]:
        findings.append("checkout revision mismatch")
    if actual_branch is not None and actual_branch != fields["branch"]:
        findings.append("checkout branch mismatch")
    if dirty:
        findings.append("checkout is dirty")

    source_launcher = Path(expand(check.get("source_launcher", "")))
    installed_launcher = Path(expand(check.get("installed_launcher", "")))
    for label, path in (("source", source_launcher), ("installed", installed_launcher)):
        if not path.is_file() or path.is_symlink():
            findings.append(f"{label} launcher missing or linked")
        elif re.fullmatch(r"[0-9a-f]{64}", expected_launcher) and digest(str(path)) != expected_launcher:
            findings.append(f"{label} launcher hash mismatch")

    skip_names = set(check.get("skip_names", [".DS_Store", "__pycache__", ".pytest_cache"]))
    skip_suffixes = set(check.get("skip_suffixes", [".pyc", ".pyo"]))
    paths: list[Path] = []
    for relative in check.get("files", []):
        path = root / relative
        if not path.is_file() or path.is_symlink():
            return result(check, "FAIL", f"missing or linked runtime file: {path}")
        paths.append(path)
    for relative in check.get("dirs", []):
        directory = root / relative
        if not directory.is_dir() or directory.is_symlink():
            return result(check, "FAIL", f"missing or linked runtime directory: {directory}")
        for path in directory.rglob("*"):
            parts = path.relative_to(root).parts
            if any(part in skip_names for part in parts) or path.suffix in skip_suffixes:
                continue
            if path.is_symlink():
                return result(check, "FAIL", f"linked runtime path: {path}")
            if path.is_file():
                paths.append(path)

    runtime_hash = hashlib.sha256()
    for path in sorted(set(paths), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        runtime_hash.update(relative.encode("utf-8") + b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                runtime_hash.update(chunk)
        runtime_hash.update(b"\0")
    actual = runtime_hash.hexdigest()
    if re.fullmatch(r"[0-9a-f]{64}", expected) and actual != expected:
        findings.append("runtime hash mismatch")
    okay = not findings
    detail = f"root={root} receipt={receipt} sha256={actual[:12]}"
    if findings:
        detail += " findings=" + "; ".join(findings)
    return result(check, "PASS" if okay else "FAIL", detail)


def read_text_paths(check: dict[str, Any]) -> tuple[list[tuple[str, str]], dict[str, Any] | None]:
    paths, missed = resolve_paths(check["paths"])
    if missed:
        return [], result(check, "FAIL", "coverage path/glob matched nothing: " + ", ".join(missed))
    out: list[tuple[str, str]] = []
    for path in paths:
        if not os.path.isfile(path):
            return [], result(check, "FAIL", f"not a readable file: {path}")
        try:
            out.append((path, Path(path).read_text(encoding="utf-8", errors="replace")))
        except OSError as exc:
            return [], result(check, "FAIL", f"read failed: {path}: {exc.__class__.__name__}")
    return out, None


def check_text_absent(check: dict[str, Any]) -> dict[str, Any]:
    items, error = read_text_paths(check)
    if error:
        return error
    findings: list[str] = []
    for path, text in items:
        for pattern in check.get("patterns", []):
            for match in re.finditer(pattern, text, re.MULTILINE):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{path}:{line} pattern={pattern!r}")
                break
    if findings:
        return result(check, "FAIL", "; ".join(findings))
    return result(check, "PASS", f"{len(items)} file(s); forbidden patterns absent")


def check_text_contract(check: dict[str, Any]) -> dict[str, Any]:
    items, error = read_text_paths(check)
    if error:
        return error
    findings: list[str] = []
    for path, text in items:
        for pattern in check.get("required", []):
            if not re.search(pattern, text, re.MULTILINE):
                findings.append(f"{path}: missing required {pattern!r}")
        for pattern in check.get("forbidden", []):
            match = re.search(pattern, text, re.MULTILINE)
            if match:
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{path}:{line}: forbidden {pattern!r}")
    if findings:
        return result(check, "FAIL", "; ".join(findings))
    return result(check, "PASS", f"{len(items)} file(s); contract satisfied")


def check_tree_symlinks(check: dict[str, Any]) -> dict[str, Any]:
    roots = [expand(p) for p in check.get("roots", [])]
    missing = [p for p in roots if not os.path.isdir(p)]
    if missing:
        return result(check, "FAIL", "missing roots: " + ", ".join(missing))
    excludes = SKIP_DIRS | set(check.get("exclude_dirs", []))
    broken: list[str] = []
    inspected = 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            kept_dirs: list[str] = []
            for name in dirnames:
                path = os.path.join(dirpath, name)
                if os.path.islink(path):
                    inspected += 1
                    if not os.path.exists(path):
                        broken.append(path)
                elif name not in excludes:
                    kept_dirs.append(name)
            dirnames[:] = kept_dirs
            for name in filenames:
                path = os.path.join(dirpath, name)
                if os.path.islink(path):
                    inspected += 1
                    if not os.path.exists(path):
                        broken.append(path)
    if broken:
        return result(check, "FAIL", f"{len(broken)} broken of {inspected}: " + ", ".join(broken[:20]))
    return result(check, "PASS", f"{inspected} symlink(s), none broken")


def check_skill_provenance(check: dict[str, Any]) -> dict[str, Any]:
    manifest_path = expand(check["manifest"])
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return result(check, "FAIL", f"cannot read provenance manifest: {manifest_path}: {exc}")
    install_root = Path(expand(manifest["install_root"]))
    if not install_root.is_dir():
        return result(check, "FAIL", f"install root missing: {install_root}")
    entries = manifest.get("skills", [])
    expected_names = {entry["name"] for entry in entries}
    actual_names = {
        path.name for path in install_root.iterdir()
        if not path.name.startswith(".") and (path.is_dir() or path.is_symlink())
    }
    findings: list[str] = []
    if expected_names != actual_names:
        missing = sorted(actual_names - expected_names)
        absent = sorted(expected_names - actual_names)
        if missing:
            findings.append("unmanifested installs=" + ",".join(missing))
        if absent:
            findings.append("manifest entries missing=" + ",".join(absent))
    for entry in entries:
        name = entry["name"]
        target = Path(expand(entry.get("target", str(install_root / name))))
        mode = entry["mode"]
        if mode == "symlink":
            source = Path(expand(entry["source"]))
            if not target.is_symlink() or os.path.realpath(target) != os.path.realpath(source):
                findings.append(f"{name}: symlink does not resolve to {source}")
        elif mode == "same_tree":
            source = Path(expand(entry["source"]))
            if not source.is_dir() or not target.is_dir():
                findings.append(f"{name}: source or install tree missing")
            elif tree_digest(str(source)) != tree_digest(str(target)):
                findings.append(f"{name}: installed tree differs from {source}")
        elif mode == "tree_hash":
            if not target.is_dir():
                findings.append(f"{name}: install tree missing")
            else:
                actual = tree_digest(str(target))
                if actual != entry["sha256"]:
                    findings.append(f"{name}: sha256={actual[:12]} expected={entry['sha256'][:12]}")
        elif mode == "installed_only":
            # Authored directly under install_root with no canonical repo source.
            # Existence is the only invariant; there is nothing to diff against.
            if not target.is_dir():
                findings.append(f"{name}: install tree missing")
        else:
            findings.append(f"{name}: unknown provenance mode {mode}")
    if findings:
        return result(check, "FAIL", "; ".join(findings))
    return result(check, "PASS", f"{len(entries)} skill install(s) covered and current via {manifest_path}")


class RecordingRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self.redirects: list[dict[str, Any]] = []

    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> urllib.request.Request | None:
        self.redirects.append({"status": code, "from": req.full_url, "to": newurl})
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def probe_url(url: str, timeout: float = 20.0,
              required_body_pattern: str | None = None,
              forbidden_body_pattern: str | None = None) -> dict[str, Any]:
    redirects = RecordingRedirectHandler()
    opener = urllib.request.build_opener(redirects, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 estate-watch-drift/1.1"),
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        },
    )
    body_match = None
    forbidden_body_match = None
    response_header_names: list[str] = []
    try:
        with opener.open(req, timeout=timeout) as response:
            status = int(response.status)
            final_url = response.geturl()
            response_header_names = sorted({name.lower() for name in response.headers.keys()})
            body = response.read(262_145)
            if required_body_pattern is not None:
                body_match = re.search(
                    required_body_pattern, body.decode("utf-8", "replace"),
                    re.IGNORECASE) is not None
            if forbidden_body_pattern is not None:
                forbidden_body_match = re.search(
                    forbidden_body_pattern, body.decode("utf-8", "replace"),
                    re.IGNORECASE) is not None
            error = ""
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        final_url = exc.geturl()
        response_header_names = sorted({name.lower() for name in (exc.headers or {}).keys()})
        error = str(exc.reason or "")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        status = 0
        final_url = url
        error = f"{exc.__class__.__name__}: {getattr(exc, 'reason', exc)}"
    return {
        "status": status,
        "final_url": final_url,
        "redirects": redirects.redirects,
        "error": error,
        "body_match": body_match,
        "forbidden_body_match": forbidden_body_match,
        "response_header_names": response_header_names,
        "verified": dt.date.today().isoformat(),
    }


def safe_url(url: str) -> str:
    """Return a URL safe for logs: status work never needs query credentials/tokens."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def evaluate_url(check: dict[str, Any], probe: dict[str, Any]) -> dict[str, Any]:
    status = int(probe["status"])
    expect = check["expect"]
    allowed = {int(x) for x in check.get("allowed_status", [])}
    final_host = (urllib.parse.urlparse(probe["final_url"]).hostname or "").lower()
    redirect_hosts = {
        (urllib.parse.urlparse(item["to"]).hostname or "").lower()
        for item in probe.get("redirects", [])
    }
    chain = " -> ".join(f"{r['status']}:{safe_url(r['to'])}" for r in probe.get("redirects", [])) or "none"
    detail = (
        f"expect={expect} status={status:03d} final={safe_url(probe['final_url'])} "
        f"redirects={chain} verified={probe['verified']}"
    )
    if probe.get("error"):
        detail += f" error={probe['error']}"
    if check.get("required_body_pattern"):
        detail += " body_marker=" + ("matched" if probe.get("body_match") else "missing")
    if check.get("forbidden_body_pattern"):
        detail += " forbidden_body=" + (
            "present" if probe.get("forbidden_body_match") else "absent")
    sanitized_probe = {
        **probe,
        "final_url": safe_url(probe["final_url"]),
        "redirects": [
            {**item, "from": safe_url(item["from"]), "to": safe_url(item["to"])}
            for item in probe.get("redirects", [])
        ],
    }
    if status == 0:
        return result(check, "UNVERIFIED", detail, probe=sanitized_probe)

    okay = status in allowed
    if expect == "public":
        permitted_hosts = {h.lower() for h in check.get("allowed_final_hosts", [])}
        if permitted_hosts and final_host not in permitted_hosts:
            okay = False
        if check.get("required_body_pattern") and probe.get("body_match") is not True:
            okay = False
        if check.get("forbidden_body_pattern") and probe.get("forbidden_body_match") is not False:
            okay = False
    elif expect in {"gated", "paused"}:
        gate_hosts = {h.lower() for h in check.get("allowed_redirect_hosts", [])}
        explicit_gate = status in {401, 403}
        redirected_to_gate = bool(gate_hosts & redirect_hosts) or final_host in gate_hosts
        access_header_evidence = any(
            name.lower().startswith("cf-access-")
            for name in probe.get("response_header_names", [])
        )
        if gate_hosts:
            # A generic origin/bot 401 or 403 is not proof that the declared
            # Cloudflare Access policy is present. Require the exact Access host
            # in the redirect/final chain or an Access-specific response header.
            okay = okay and (redirected_to_gate or access_header_evidence)
        else:
            okay = okay and explicit_gate
        detail += " gate_evidence=" + (
            "host" if redirected_to_gate else "header" if access_header_evidence else "missing"
        )
    elif expect in {"unpublished", "retired"}:
        # A planned/unpublished or retired surface unexpectedly becoming reachable
        # is state drift. Keep the labels distinct so a never-launched proof is not
        # misreported as a dead lead or decommissioned deployment.
        okay = status in allowed
    else:
        return result(check, "FAIL", f"unknown URL expectation: {expect}")

    return result(check, "PASS" if okay else "FAIL", detail, probe=sanitized_probe)


def run_check(check: dict[str, Any], network: bool = False) -> dict[str, Any]:
    handlers = {
        "path_exists": check_path_exists,
        "same_file": check_same_file,
        "git_tracked": check_git_tracked,
        "runtime_receipt": check_runtime_receipt,
        "text_absent": check_text_absent,
        "text_contract": check_text_contract,
        "tree_symlinks": check_tree_symlinks,
        "skill_provenance": check_skill_provenance,
    }
    if check["type"] == "url":
        if not network:
            return result(check, "SKIP", "network disabled; rerun with --network")
        return evaluate_url(check, probe_url(
            check["url"], float(check.get("timeout", 20)),
            check.get("required_body_pattern"),
            check.get("forbidden_body_pattern")))
    handler = handlers.get(check["type"])
    if not handler:
        return result(check, "FAIL", f"unknown check type: {check['type']}")
    try:
        return handler(check)
    except Exception as exc:  # keep a full estate audit running after one bad check
        return result(check, "FAIL", f"checker error: {exc.__class__.__name__}: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=default_manifest())
    parser.add_argument("--network", action="store_true", help="enable read-only URL probes")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--only", action="append", default=[], help="run only a check id (repeatable)")
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    checks = manifest.get("checks", [])
    if args.only:
        wanted = set(args.only)
        checks = [c for c in checks if c.get("id") in wanted]
        missing = wanted - {c.get("id") for c in checks}
        if missing:
            print("unknown check id(s): " + ", ".join(sorted(missing)), file=sys.stderr)
            return 2

    results = [run_check(check, network=args.network) for check in checks]
    if args.json_output:
        print(json.dumps({"verified": dt.date.today().isoformat(), "results": results}, indent=2))
    else:
        for item in results:
            print(f"{item['state']:<4} {item['id']}: {item['detail']}")
        counts = {
            state: sum(r["state"] == state for r in results)
            for state in ("PASS", "FAIL", "UNVERIFIED", "SKIP")
        }
        print(
            f"summary: {counts['PASS']} pass, {counts['FAIL']} fail, "
            f"{counts['UNVERIFIED']} unverified, {counts['SKIP']} skipped"
        )
    return 1 if any(r["state"] in {"FAIL", "UNVERIFIED"} for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
