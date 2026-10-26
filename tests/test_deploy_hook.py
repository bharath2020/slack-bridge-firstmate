"""The Slack CLI deploy hook, against fake `security` and `slack` binaries."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from slack_bridge.config import load_secrets

from .helpers import FAKES

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "scripts" / "slack-deploy-hook.sh"
BOT = "xoxb-111-222-abcDEF"
APP = "xapp-1-A111-333-def456"
AUTH_LIST = "myteam\nTeam ID: T0123\nUser ID: U0CAPTAIN\nLast Updated: today"


class DeployHookTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def run_hook(self, **env):
        base = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.dir),
            "USER": "captain",
            "FAKE_SECURITY_DIR": str(self.dir),
            "SLACK_BRIDGE_SECURITY": str(FAKES / "security"),
            "SLACK_BRIDGE_SLACK": str(FAKES / "slack"),
            "SLACK_BRIDGE_NO_INSTALL": "1",
            "FAKE_SLACK_AUTH_LIST": AUTH_LIST,
        }
        base.update(env)
        return subprocess.run([str(HOOK)], env=base, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, check=False)

    def store(self):
        p = self.dir / "store.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def argv_log(self):
        return (self.dir / "argv.log").read_text() if (self.dir / "argv.log").exists() else ""

    def test_stores_tokens_and_captain_without_exposing_them(self):
        proc = self.run_hook(SLACK_BOT_TOKEN=BOT, SLACK_APP_TOKEN=APP)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.store(), {
            "captain/slack-bridge.bot-token": BOT,
            "captain/slack-bridge.app-token": APP,
            "captain/slack-bridge.captain-user-id": "U0CAPTAIN",
        })
        # Tokens went over stdin: never in any argv, never in the hook's output.
        for secret in (BOT, APP):
            self.assertNotIn(secret, self.argv_log())
            self.assertNotIn(secret, proc.stdout + proc.stderr)

    def test_missing_bot_token_fails(self):
        proc = self.run_hook(SLACK_APP_TOKEN=APP)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.store(), {})

    def test_missing_app_token_explains_manual_step(self):
        proc = self.run_hook(SLACK_BOT_TOKEN=BOT)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("connections:write", proc.stdout)
        self.assertIn("slack-bridge.app-token", proc.stdout)

    def test_missing_app_token_keeps_existing_one(self):
        (self.dir / "store.json").write_text(json.dumps(
            {"captain/slack-bridge.app-token": APP}))
        proc = self.run_hook(SLACK_BOT_TOKEN=BOT)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self.store()["captain/slack-bridge.app-token"], APP)

    def test_ambiguous_login_keeps_stored_captain(self):
        (self.dir / "store.json").write_text(json.dumps(
            {"captain/slack-bridge.captain-user-id": "U0STORED"}))
        two = AUTH_LIST + "\nother\nUser ID: U0OTHER"
        proc = self.run_hook(SLACK_BOT_TOKEN=BOT, SLACK_APP_TOKEN=APP,
                             FAKE_SLACK_AUTH_LIST=two)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self.store()["captain/slack-bridge.captain-user-id"], "U0STORED")

    def test_malformed_token_is_refused(self):
        proc = self.run_hook(SLACK_BOT_TOKEN='xoxb-1" -s evil', SLACK_APP_TOKEN=APP)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.store(), {})


class EnvTokensTest(unittest.TestCase):
    """app.py (slack run) takes the CLI's tokens; nothing else does."""

    def test_env_tokens_only_when_opted_in(self):
        env = {"SLACK_BRIDGE_CAPTAIN_ID": "U1", "SLACK_BRIDGE_TOKENS_FROM_ENV": "1",
               "SLACK_BOT_TOKEN": BOT, "SLACK_APP_TOKEN": APP}
        s = load_secrets(env=env)
        self.assertEqual((s.bot_token, s.app_token, s.captain_id), (BOT, APP, "U1"))
        self.assertNotIn(BOT, repr(s))


if __name__ == "__main__":
    unittest.main()
