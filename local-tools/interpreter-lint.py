#!/usr/bin/env python3
"""interpreter-lint — fail on any launchd plist, or any script a plist launches,
that names the python.org framework python (/Library/Frameworks/Python.framework).

Why: that build exists on one machine only. On 2026-09-23 the Mac Studio move
found 7 plists and 5 launched scripts pinned to it (6 mlb jobs, deliverables-sweep,
the outbound CRM launcher, notes-vault's launch and session-log scripts); each
failed on the Studio, which has Homebrew 3.12. The portable pins are the repo's
own .venv or /opt/homebrew/bin/python3.12 (present on both Macs).

Scans ~/Library/LaunchAgents/com.jeff*.plist (and *.plist.disabled): Program,
ProgramArguments and EnvironmentVariables values, plus the text of every argv
element that is an existing script file outside the system and Homebrew trees.
Comment lines are skipped. A line that names the framework ON PURPOSE as one
candidate in a probe that also has a portable fallback (lantern-cloud's backup
script tries it, then a venv, keeping the first that imports boto3) carries the
marker `# interp-lint: probe` and is counted, not flagged: the marker is visible
in the diff and in this lint's non-quiet output, so it is classified noise,
never filtered noise.

Output: one `label: code: detail` line per finding, codes `framework-pin` and
`plist-unreadable` (a plist neither plistlib nor plutil can read, or whose top
level is not a dict: it cannot be proven clean, so it fails). Exit 1 on findings,
0 clean, 2 on an internal error (never mistake a crash for clean).
Runs inside fleet-sentinel (4x/day) and the weekly projects-hygiene report.
Python 3.9 stdlib only (fleet-sentinel's PY is /usr/bin/python3).

Usage: interpreter-lint.py [--agents-dir DIR] [--quiet]
"""
import argparse
import os
import plistlib
import subprocess
import sys
import traceback

FRAMEWORK = "/Library/Frameworks/Python.framework"
PROBE_MARKER = "interp-lint: probe"
SYSTEM_PREFIXES = ("/bin/", "/usr/bin/", "/usr/sbin/", "/sbin/", "/opt/homebrew/",
                   "/usr/local/", "/System/", "/Applications/")


def load_plist(path):
    """plistlib first, then plutil (launchd's own reader) for XML plistlib rejects,
    such as a comment containing '--'. Returns (dict, None) or (None, reason)."""
    try:
        with open(path, "rb") as f:
            pl = plistlib.load(f)
    except Exception as e1:
        try:
            r = subprocess.run(["/usr/bin/plutil", "-convert", "xml1", "-o", "-", path],
                               capture_output=True, timeout=20)
            if r.returncode != 0:
                err = r.stderr.decode("utf-8", errors="ignore").strip() or ("exit %d" % r.returncode)
                return None, "plistlib: %s; plutil: %s" % (e1, err)
            pl = plistlib.loads(r.stdout)
        except Exception as e2:
            return None, "plistlib: %s; plutil fallback: %s" % (e1, e2)
    if not isinstance(pl, dict):
        return None, "top-level element is %s, not a dict" % type(pl).__name__
    return pl, None


def script_hits(path):
    """(hits, probes): hits = (lineno, text) for non-comment lines naming the
    framework; probes = linenos that carry the probe marker. Binaries give ([], [])."""
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
        if b"\0" in head:
            return [], []
        with open(path, "r", errors="ignore") as f:
            lines = f.readlines()
    except OSError:
        return [], []
    hits, probes = [], []
    for n, line in enumerate(lines, 1):
        if line.lstrip().startswith("#") or FRAMEWORK not in line:
            continue
        if PROBE_MARKER in line:
            probes.append(n)
        else:
            hits.append((n, line.strip()))
    return hits, probes


def scan(agents_dir):
    """Returns (findings, probes): findings = (label, code, detail); probes = (label, path, lineno)."""
    findings, probes = [], []
    names = sorted(os.listdir(agents_dir))
    for name in names:
        if not name.startswith("com.jeff"):
            continue
        if not (name.endswith(".plist") or name.endswith(".plist.disabled")):
            continue
        path = os.path.join(agents_dir, name)
        stem = name.split(".plist")[0]
        pl, reason = load_plist(path)
        if pl is None:
            findings.append((stem, "plist-unreadable", "%s: %s" % (path, reason)))
            continue
        label = str(pl.get("Label") or stem)
        program = pl.get("Program")
        argv = [str(a) for a in (pl.get("ProgramArguments") or [])]
        if program and FRAMEWORK in str(program):
            findings.append((label, "framework-pin", "Program names the framework: %s" % program))
        for i, a in enumerate(argv):
            if FRAMEWORK in a:
                findings.append((label, "framework-pin", "ProgramArguments[%d] names the framework: %s" % (i, a)))
        env = pl.get("EnvironmentVariables") or {}
        if isinstance(env, dict):
            for k, v in env.items():
                if FRAMEWORK in str(v):
                    findings.append((label, "framework-pin", "EnvironmentVariables[%s] names the framework: %s" % (k, v)))
        seen = set()
        for a in ([str(program)] if program else []) + argv:
            p = os.path.expanduser(a)
            if p in seen or not os.path.isabs(p) or p.startswith(SYSTEM_PREFIXES):
                continue
            seen.add(p)
            if not os.path.isfile(p):
                continue
            hits, marked = script_hits(p)
            for n, text in hits:
                findings.append((label, "framework-pin", "%s:%d names the framework: %s" % (p, n, text[:140])))
            for n in marked:
                probes.append((label, p, n))
    return findings, probes


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--agents-dir", default=os.path.expanduser("~/Library/LaunchAgents"))
    ap.add_argument("--quiet", action="store_true", help="print findings only")
    args = ap.parse_args()
    try:
        findings, probes = scan(args.agents_dir)
    except Exception:
        sys.stderr.write("interpreter-lint: internal error\n" + traceback.format_exc())
        return 2
    for label, code, detail in findings:
        print("%s: %s: %s" % (label, code, detail))
    if not args.quiet:
        for label, p, n in probes:
            print("%s: probe-marker (allowed, not a finding): %s:%d" % (label, p, n))
        if not findings:
            print("interpreter-lint: clean (%s; %d probe-marked line(s))" % (args.agents_dir, len(probes)))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
