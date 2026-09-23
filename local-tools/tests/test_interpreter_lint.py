#!/usr/bin/env python3
"""interpreter-lint must flag exactly the plists (and launched scripts) that name the
python.org framework python, read plists the way launchd does, never mistake a crash
for clean, and honour the probe marker. Mutation checks: drop any bad fixture from
EXPECTED and a test fails; make the lint skip EnvironmentVariables, or drop the
plutil fallback, or forget the isinstance guard, and a test fails.

Run: python3 ~/.claude/bin/tests/test_interpreter_lint.py
"""
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LINT = os.path.join(os.path.dirname(HERE), "interpreter-lint.py")
if not os.path.isfile(LINT):
    LINT = os.path.expanduser("~/.claude/bin/interpreter-lint.py")
FW = "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"


def write_plist(d, label, argv, env=None, suffix=".plist"):
    pl = {"Label": label, "ProgramArguments": argv}
    if env:
        pl["EnvironmentVariables"] = env
    with open(os.path.join(d, label + suffix), "wb") as f:
        plistlib.dump(pl, f)


def write_raw(d, name, text):
    with open(os.path.join(d, name), "w") as f:
        f.write(text)


def write_script(d, name, body):
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write(body)
    os.chmod(p, 0o755)
    return p


def run(agents, quiet=True):
    cmd = [sys.executable, LINT, "--agents-dir", agents] + (["--quiet"] if quiet else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    rows = [line.split(": ", 2) for line in r.stdout.splitlines() if line.strip()]
    return r, sorted({(row[0], row[1]) for row in rows if len(row) == 3})


class InterpreterLint(unittest.TestCase):
    def test_flags_exactly_the_framework_users(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents = os.path.join(tmp, "LaunchAgents"); os.makedirs(agents)
            clean_sh = write_script(tmp, "clean.sh", "#!/bin/bash\nexec /opt/homebrew/bin/python3.12 job.py\n")
            bad_sh = write_script(tmp, "bad.sh", "#!/bin/bash\nPY=%s\n$PY job.py\n" % FW)
            comment_sh = write_script(tmp, "comment.sh", "#!/bin/bash\n# was %s once\nexec python3 job.py\n" % FW)
            probe_sh = write_script(tmp, "probe.sh", "#!/bin/bash\nfor c in %s ~/.venvs/x/bin/python; do  # interp-lint: probe\n  [ -x \"$c\" ] && break\ndone\n" % FW)
            write_plist(agents, "com.jeffpinto.clean", ["/bin/bash", clean_sh])
            write_plist(agents, "com.jeffpinto.env-bad", ["/bin/bash", clean_sh],
                        env={"PATH": "/Library/Frameworks/Python.framework/Versions/3.12/bin:/usr/bin:/bin"})
            write_plist(agents, "com.jeffpinto.argv-bad", [FW, "job.py"])
            write_plist(agents, "com.jeffpinto.script-bad", ["/bin/bash", bad_sh])
            write_plist(agents, "com.jeffpinto.comment-only", ["/bin/bash", comment_sh])
            write_plist(agents, "com.jeffpinto.probe-ok", ["/bin/bash", probe_sh])
            write_plist(agents, "com.jeffpinto.disabled-bad", [FW, "job.py"], suffix=".plist.disabled")
            write_plist(agents, "com.google.not-ours", [FW, "job.py"])
            # launchd reads this; plistlib alone rejects the '--' inside the comment
            write_raw(agents, "com.jeffpinto.dashes-bad.plist",
                      '<?xml version="1.0" encoding="UTF-8"?>\n'
                      '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                      '<plist version="1.0"><dict>\n<!-- compounds daily -- the prefill -->\n'
                      '<key>Label</key><string>com.jeffpinto.dashes-bad</string>\n'
                      '<key>ProgramArguments</key><array><string>%s</string><string>job.py</string></array>\n'
                      '</dict></plist>\n' % FW)
            # valid plist whose top level is not a dict: cannot be proven clean
            write_raw(agents, "com.jeffpinto.array-top.plist",
                      '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><array><string>x</string></array></plist>\n')
            r, pairs = run(agents)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertEqual(r.stderr, "")
            self.assertEqual(pairs, sorted([
                ("com.jeffpinto.env-bad", "framework-pin"),
                ("com.jeffpinto.argv-bad", "framework-pin"),
                ("com.jeffpinto.script-bad", "framework-pin"),
                ("com.jeffpinto.disabled-bad", "framework-pin"),
                ("com.jeffpinto.dashes-bad", "framework-pin"),
                ("com.jeffpinto.array-top", "plist-unreadable"),
            ]), r.stdout)
            self.assertIn("bad.sh:2 names the framework", r.stdout)
            self.assertNotIn("probe.sh", r.stdout)
            r2, _ = run(agents, quiet=False)
            self.assertIn("com.jeffpinto.probe-ok: probe-marker (allowed, not a finding)", r2.stdout)

    def test_clean_dir_exits_zero_and_quiet_prints_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents = os.path.join(tmp, "LaunchAgents"); os.makedirs(agents)
            clean_sh = write_script(tmp, "clean.sh", "#!/bin/bash\nexec python3 job.py\n")
            write_plist(agents, "com.jeffpinto.clean", ["/bin/bash", clean_sh])
            r, pairs = run(agents)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
            r2, _ = run(agents, quiet=False)
            self.assertIn("interpreter-lint: clean", r2.stdout)

    def test_internal_error_exits_two_not_clean(self):
        r, _ = run(os.path.join(tempfile.gettempdir(), "no-such-dir-for-interp-lint"))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("internal error", r.stderr)
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
