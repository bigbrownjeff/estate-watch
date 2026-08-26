from pathlib import Path
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "watch.yml"


class WorkflowContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_redirects_are_followed_and_terminal_url_is_sanitized(self):
        self.assertIn("curl -sS -L --max-redirs 5", self.text)
        self.assertIn("effective_safe=${effective%%\\?*}", self.text)
        self.assertIn("effective_safe=${effective_safe%%\\#*}", self.text)
        self.assertNotIn("$effective; expected", self.text)
        self.assertNotIn("$effective)\"", self.text)

    def test_blue_camel_soft_launch_demos_are_public_contracts(self):
        for name in (
            "brand-safety-demo",
            "cc-compliance-demo",
            "camh-subgroup-demo",
        ):
            line = next(line for line in self.text.splitlines() if f'"{name}"' in line)
            self.assertTrue(line.rstrip().endswith("public"), line)

    def test_crm_requires_real_access_semantics(self):
        line = next(line for line in self.text.splitlines()
                    if '"crm.bluecamel"' in line)
        self.assertIn(' gated "cold-waterfall-c527.cloudflareaccess.com"', line)
        self.assertIn('gated:401|gated:403', self.text)
        self.assertIn(
            'if [ -z "$access_host" ] || [ "$effective_host" = "$access_host" ]; then healthy=1; fi;;',
            self.text,
        )
        self.assertNotIn('gated:401|gated:403)\n                healthy=1;;', self.text)
        self.assertNotIn(
            '"https://crm.bluecamelconsulting.com/" reachable', self.text)

    def test_blue_camel_llc_site_is_public_with_content_marker(self):
        line = next(line for line in self.text.splitlines()
                    if '"tunnel:bluecamel"' in line)
        self.assertIn(' public "" "Blue Camel"', line)
        self.assertNotIn(
            '"https://bluecamelconsulting.com/" reachable', self.text)
        self.assertIn('grep -Fqi -- "$marker" "$body"', self.text)


if __name__ == "__main__":
    unittest.main()
