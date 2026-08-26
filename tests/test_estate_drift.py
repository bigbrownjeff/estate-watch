import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "estate_drift.py"
SPEC = importlib.util.spec_from_file_location("estate_drift", SCRIPT)
estate_drift = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(estate_drift)


class LocalChecksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_file_detects_content_drift(self):
        source = self.root / "source"
        target = self.root / "target"
        source.write_text("current", encoding="utf-8")
        target.write_text("current", encoding="utf-8")
        check = {"id": "copy", "type": "same_file", "source": str(source), "target": str(target)}
        self.assertEqual("PASS", estate_drift.run_check(check)["state"])
        target.write_text("stale", encoding="utf-8")
        self.assertEqual("FAIL", estate_drift.run_check(check)["state"])

    def test_text_absent_fails_closed_on_missing_coverage(self):
        check = {
            "id": "coverage",
            "type": "text_absent",
            "paths": [str(self.root / "missing-*.md")],
            "patterns": ["old-root"],
        }
        outcome = estate_drift.run_check(check)
        self.assertEqual("FAIL", outcome["state"])
        self.assertIn("matched nothing", outcome["detail"])

    def test_text_absent_reports_file_and_line_without_echoing_content(self):
        path = self.root / "instructions.md"
        path.write_text("ok\nold-root\n", encoding="utf-8")
        check = {"id": "text", "type": "text_absent", "paths": [str(path)], "patterns": ["old-root"]}
        outcome = estate_drift.run_check(check)
        self.assertEqual("FAIL", outcome["state"])
        self.assertIn(":2", outcome["detail"])

    def test_broken_symlink_is_not_a_clean_result(self):
        os.symlink(self.root / "missing", self.root / "link")
        check = {"id": "links", "type": "tree_symlinks", "roots": [str(self.root)]}
        outcome = estate_drift.run_check(check)
        self.assertEqual("FAIL", outcome["state"])
        self.assertIn("broken", outcome["detail"])

    def test_skill_provenance_fails_on_unmanifested_copy(self):
        installs = self.root / "installs"
        installs.mkdir()
        source = self.root / "source"
        source.mkdir()
        (source / "SKILL.md").write_text("current", encoding="utf-8")
        os.symlink(source, installs / "known")
        (installs / "unknown").mkdir()
        manifest = self.root / "provenance.json"
        manifest.write_text(json.dumps({
            "version": 1,
            "install_root": str(installs),
            "skills": [{"name": "known", "mode": "symlink", "source": str(source)}],
        }), encoding="utf-8")
        check = {"id": "skills", "type": "skill_provenance", "manifest": str(manifest)}
        outcome = estate_drift.run_check(check)
        self.assertEqual("FAIL", outcome["state"])
        self.assertIn("unmanifested", outcome["detail"])

    def test_tree_digest_ignores_python_bytecode_caches(self):
        left = self.root / "left"
        right = self.root / "right"
        for directory in (left, right):
            directory.mkdir()
            (directory / "SKILL.md").write_text("current", encoding="utf-8")
        cache = left / "scripts" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "validator.cpython-314.pyc").write_bytes(b"transient")
        self.assertEqual(
            estate_drift.tree_digest(str(left)),
            estate_drift.tree_digest(str(right)),
        )

    def test_git_tracked_requires_clean_versioned_authority(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Estate Test"], check=True)
        authority = repo / "workups" / "index.json"
        authority.parent.mkdir()
        authority.write_text("{}\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "authority"], check=True,
                       capture_output=True, text=True)
        check = {
            "id": "tracked",
            "type": "git_tracked",
            "repo": str(repo),
            "paths": ["workups/index.json"],
        }
        self.assertEqual("PASS", estate_drift.run_check(check)["state"])
        authority.write_text('{"changed": true}\n', encoding="utf-8")
        self.assertEqual("FAIL", estate_drift.run_check(check)["state"])

    def test_runtime_receipt_requires_release_provenance_not_only_a_hash(self):
        runtime = self.root / "runtime"
        (runtime / "crm").mkdir(parents=True)
        (runtime / "local-tools").mkdir()
        (runtime / "crm" / "web.py").write_text("current", encoding="utf-8")
        source_launcher = runtime / "local-tools" / "launcher.sh"
        installed_launcher = self.root / "installed-launcher.sh"
        source_launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        installed_launcher.write_text("#!/bin/sh\n", encoding="utf-8")

        def git(*args):
            return subprocess.run(
                ["git", "-C", str(runtime), *args], check=True,
                capture_output=True, text=True,
            ).stdout.strip()

        git("init", "-b", "main")
        git("config", "user.email", "estate-watch@example.test")
        git("config", "user.name", "Estate Watch Test")
        git("add", ".")
        git("commit", "-m", "runtime")
        revision = git("rev-parse", "HEAD")

        receipt = self.root / "approved-runtime.receipt"
        check = {
            "id": "runtime",
            "type": "runtime_receipt",
            "root": str(runtime),
            "receipt": str(receipt),
            "schema": "outbound-crm-runtime-approval.v1",
            "branch": "main",
            "source_launcher": str(source_launcher),
            "installed_launcher": str(installed_launcher),
            "python": sys.executable,
            "test_command": "test-suite",
            "dirs": ["crm"],
            "files": ["local-tools/launcher.sh"],
        }
        paths = [runtime / "crm" / "web.py", source_launcher]
        runtime_hash = estate_drift.hashlib.sha256()
        for path in sorted(paths, key=lambda item: item.relative_to(runtime).as_posix()):
            relative = path.relative_to(runtime).as_posix()
            runtime_hash.update(relative.encode("utf-8") + b"\0")
            runtime_hash.update(path.read_bytes())
            runtime_hash.update(b"\0")
        launcher_hash = estate_drift.digest(str(source_launcher))

        fields = {
            "schema": "outbound-crm-runtime-approval.v1",
            "repo": str(runtime.resolve()),
            "revision": revision,
            "branch": "main",
            "runtime_sha256": runtime_hash.hexdigest(),
            "launcher_sha256": launcher_hash,
            "python": sys.executable,
            "test_command": "test-suite",
            "approved_at": "2026-08-06T23:30:00Z",
        }

        def write_receipt(**overrides):
            values = {**fields, **overrides}
            receipt.write_text(
                "".join(f"{key}={value}\n" for key, value in values.items()),
                encoding="ascii",
            )

        write_receipt()
        self.assertEqual("PASS", estate_drift.run_check(check)["state"])

        write_receipt(revision="0" * 40)
        self.assertEqual("FAIL", estate_drift.run_check(check)["state"])
        write_receipt(branch="feature")
        self.assertEqual("FAIL", estate_drift.run_check(check)["state"])
        write_receipt()
        installed_launcher.write_text("different\n", encoding="utf-8")
        self.assertEqual("FAIL", estate_drift.run_check(check)["state"])
        installed_launcher.write_text("#!/bin/sh\n", encoding="utf-8")

        receipt.write_text(runtime_hash.hexdigest() + "\n", encoding="ascii")
        self.assertEqual("FAIL", estate_drift.run_check(check)["state"])
        write_receipt()
        paths[0].write_text("changed", encoding="utf-8")
        self.assertEqual("FAIL", estate_drift.run_check(check)["state"])


class UrlSemanticsTest(unittest.TestCase):
    def test_url_output_strips_query_and_fragment(self):
        self.assertEqual(
            "https://access.example.test/login",
            estate_drift.safe_url("https://access.example.test/login?token=sensitive#fragment"),
        )

    def test_public_requires_allowed_terminal_status(self):
        check = {"id": "public", "type": "url", "expect": "public", "allowed_status": [200]}
        probe = {"status": 404, "final_url": "https://example.test/", "redirects": [], "verified": "2026-08-06", "error": ""}
        self.assertEqual("FAIL", estate_drift.evaluate_url(check, probe)["state"])

    def test_public_marker_rejects_a_challenge_page_even_on_200(self):
        check = {
            "id": "public-marker",
            "type": "url",
            "expect": "public",
            "allowed_status": [200],
            "required_body_pattern": "Blue Camel",
        }
        probe = {
            "status": 200,
            "final_url": "https://bluecamelconsulting.com/",
            "redirects": [],
            "verified": "2026-08-06",
            "error": "",
            "body_match": False,
        }
        self.assertEqual("FAIL", estate_drift.evaluate_url(check, probe)["state"])
        probe["body_match"] = True
        self.assertEqual("PASS", estate_drift.evaluate_url(check, probe)["state"])

    def test_public_forbidden_body_marker_fails_closed(self):
        check = {
            "id": "public-indexing",
            "type": "url",
            "expect": "public",
            "allowed_status": [200],
            "forbidden_body_pattern": "noindex",
        }
        probe = {
            "status": 200,
            "final_url": "https://example.test/",
            "redirects": [],
            "verified": "2026-08-06",
            "error": "",
            "forbidden_body_match": True,
        }
        self.assertEqual("FAIL", estate_drift.evaluate_url(check, probe)["state"])
        probe["forbidden_body_match"] = False
        self.assertEqual("PASS", estate_drift.evaluate_url(check, probe)["state"])

    def test_gated_does_not_accept_an_ordinary_public_200(self):
        check = {
            "id": "gated",
            "type": "url",
            "expect": "gated",
            "allowed_status": [200, 401, 403],
            "allowed_redirect_hosts": ["access.example.test"],
        }
        public_probe = {"status": 200, "final_url": "https://app.example.test/", "redirects": [], "verified": "2026-08-06", "error": ""}
        gated_probe = {"status": 200, "final_url": "https://access.example.test/login", "redirects": [{"status": 302, "from": "https://app.example.test/", "to": "https://access.example.test/login"}], "verified": "2026-08-06", "error": ""}
        self.assertEqual("FAIL", estate_drift.evaluate_url(check, public_probe)["state"])
        self.assertEqual("PASS", estate_drift.evaluate_url(check, gated_probe)["state"])

    def test_paused_surface_preserves_state_but_requires_access_evidence(self):
        check = {
            "id": "paused",
            "type": "url",
            "expect": "paused",
            "allowed_status": [200, 401, 403],
            "allowed_redirect_hosts": ["access.example.test"],
        }
        public_probe = {
            "status": 200,
            "final_url": "https://paused.example.test/",
            "redirects": [],
            "verified": "2026-08-06",
            "error": "",
        }
        gated_probe = {
            **public_probe,
            "final_url": "https://access.example.test/login",
            "redirects": [{
                "status": 302,
                "from": "https://paused.example.test/",
                "to": "https://access.example.test/login",
            }],
        }
        self.assertEqual("FAIL", estate_drift.evaluate_url(
            check, public_probe)["state"])
        self.assertEqual("PASS", estate_drift.evaluate_url(
            check, gated_probe)["state"])

    def test_declared_access_host_rejects_generic_origin_denial(self):
        check = {
            "id": "gated",
            "type": "url",
            "expect": "gated",
            "allowed_status": [200, 401, 403],
            "allowed_redirect_hosts": ["access.example.test"],
        }
        generic_denial = {
            "status": 403,
            "final_url": "https://app.example.test/",
            "redirects": [],
            "response_header_names": ["cf-ray"],
            "verified": "2026-08-06",
            "error": "Forbidden",
        }
        access_denial = {
            **generic_denial,
            "response_header_names": ["cf-access-denied-reason"],
        }
        self.assertEqual("FAIL", estate_drift.evaluate_url(check, generic_denial)["state"])
        self.assertEqual("PASS", estate_drift.evaluate_url(check, access_denial)["state"])

    def test_retired_surface_resurrection_fails(self):
        check = {"id": "retired", "type": "url", "expect": "retired", "allowed_status": [404, 410]}
        probe = {"status": 200, "final_url": "https://old.example.test/", "redirects": [], "verified": "2026-08-06", "error": ""}
        self.assertEqual("FAIL", estate_drift.evaluate_url(check, probe)["state"])

    def test_network_failure_is_unverified_not_retirement_evidence(self):
        check = {"id": "retired", "type": "url", "expect": "retired", "allowed_status": [0, 404, 410]}
        probe = {
            "status": 0,
            "final_url": "https://old.example.test/",
            "redirects": [],
            "verified": "2026-08-06",
            "error": "URLError: resolver unavailable",
        }
        self.assertEqual("UNVERIFIED", estate_drift.evaluate_url(check, probe)["state"])

    def test_unpublished_surface_is_not_labeled_retired(self):
        check = {"id": "planned", "type": "url", "expect": "unpublished", "allowed_status": [404]}
        probe = {"status": 404, "final_url": "https://planned.example.test/", "redirects": [], "verified": "2026-08-06", "error": ""}
        outcome = estate_drift.evaluate_url(check, probe)
        self.assertEqual("PASS", outcome["state"])
        self.assertIn("expect=unpublished", outcome["detail"])


class CliTest(unittest.TestCase):
    def test_network_checks_skip_without_opt_in(self):
        manifest = self.root_manifest({
            "version": 1,
            "checks": [{"id": "url", "type": "url", "url": "https://example.test", "expect": "public", "allowed_status": [200]}],
        })
        self.assertEqual(0, estate_drift.main(["--manifest", str(manifest)]))

    def root_manifest(self, payload):
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        self.addCleanup(lambda: os.unlink(tmp.name))
        json.dump(payload, tmp)
        tmp.close()
        return Path(tmp.name)


if __name__ == "__main__":
    unittest.main()
