"""estate-pulse check F: which cloud-machine failure issues count as live."""
import importlib.util, os, unittest
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("estate_pulse", os.path.join(HERE, "..", "estate-pulse.py"))
ep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ep)

NOW = datetime(2026, 11, 18, 18, 0, tzinfo=timezone.utc)


def issue(n, title, hours_ago):
    ts = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"number": n, "title": title, "updatedAt": ts, "url": "u%d" % n}


class CloudIssues(unittest.TestCase):
    def live(self, issues, active_h=6):
        return [n for (n, _, _) in ep.cloud_failure_issues(issues, NOW.timestamp(), active_h)]

    def test_recent_cloud_issue_is_live(self):
        self.assertEqual(self.live([issue(1, "cloud: x.service failed on host", 1)]), [1])

    def test_old_cloud_issue_is_not_live(self):
        self.assertEqual(self.live([issue(2, "cloud: x.service failed on host", 7)]), [])

    def test_other_titles_are_ignored(self):
        self.assertEqual(self.live([issue(3, "the cloud: a design note", 1), issue(4, "bug: search", 1)]), [])

    def test_bad_timestamp_is_skipped_not_fatal(self):
        self.assertEqual(self.live([{"number": 5, "title": "cloud: y failed on h", "updatedAt": "nope"}]), [])


if __name__ == "__main__":
    unittest.main()
