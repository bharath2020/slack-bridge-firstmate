"""The audit must find deleted secrets without emitting matched values."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class AuditTest(unittest.TestCase):
    def test_all_history_includes_deleted_file_and_output_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.check_output(["git", "-C", str(root), *args], text=True)
            git("init", "-q", "-b", "main")
            git("config", "user.name", "Test Fixture")
            git("config", "user.email", "fixture@example.invalid")
            token = "xoxb-" + "synthetic-history-fixture"
            secret = root / ".env"
            secret.write_text("SLACK_BOT_TOKEN=" + token)
            git("add", ".env")
            git("-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
            first = git("rev-parse", "HEAD").strip()
            secret.unlink()
            git("add", "-u")
            git("-c", "commit.gpgsign=false", "commit", "-qm", "delete fixture")
            (root / "scripts").mkdir()
            script = root / "scripts/audit-history.py"
            shutil.copy(REPO / "scripts/audit-history.py", script)
            result = subprocess.run(["python3", str(script)], capture_output=True,
                                    text=True, check=False, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(token, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["commits"], 2)
            self.assertIn({"path": ".env", "kind": "slack-token-like",
                           "example_commit": first, "commit_count": 1}, report["findings"])
            self.assertNotIn(".env", git("ls-tree", "--name-only", "HEAD").splitlines())
