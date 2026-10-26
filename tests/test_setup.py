"""scripts/setup.sh end to end, against fakes only: a temp HOME and a temp
copy of the scripts, fake `slack`, `security`, `launchctl`, install-service.sh and
bin/slack-bridge. Nothing here reaches Slack, the Keychain or launchd."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from slack_bridge.config import Config

from .helpers import FAKES

REPO = Path(__file__).resolve().parent.parent
BOT = "xoxb-111-222-abcDEF"
APP = "xapp-1-A111-333-def456"
ONE = "myteam\nTeam ID: T0123\nUser ID: U0CAPTAIN\nLast Updated: today"
TWO = ONE + "\n\nother (Team ID: T0999)\nUser ID: U0OTHER\nLast Updated: today"


class SetupTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name).resolve()
        self.home = self.dir / "home"
        self.home.mkdir()
        # A copy of the scripts, so `slack deploy` writes .slack/apps.json
        # here and not in the real checkout.
        self.repo = self.dir / "repo"
        (self.repo / ".slack").mkdir(parents=True)
        shutil.copytree(REPO / "scripts", self.repo / "scripts")
        self.fm = self.home / "fm"
        (self.fm / "bin").mkdir(parents=True)
        shutil.copy(FAKES / "fm-inbox.sh", self.fm / "bin" / "fm-inbox.sh")
        (self.dir / "launchctl.json").write_text(json.dumps({"loaded": True}))

    def tearDown(self):
        self._tmp.cleanup()

    def run_setup(self, answers="", **env):
        base = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "USER": "captain",
            "PYTHON": sys.executable,
            "FAKE_SECURITY_DIR": str(self.dir),
            "FAKE_SYS_DIR": str(self.dir),
            "FAKE_SLACK_DIR": str(self.dir),
            "FAKE_SLACK_LOGIN_AS": ONE,
            "FAKE_SLACK_BOT": BOT,
            "FAKE_SLACK_APP": APP,
            "SLACK_BRIDGE_SLACK": str(FAKES / "slack"),
            "SLACK_BRIDGE_HERDR": str(FAKES / "herdr"),
            "SLACK_BRIDGE_SECURITY": str(FAKES / "security"),
            "SLACK_BRIDGE_LAUNCHCTL": str(FAKES / "launchctl"),
            "SLACK_BRIDGE_INSTALL": str(FAKES / "install"),
            "SLACK_BRIDGE_CLI": str(FAKES / "slack-bridge"),
            "SLACK_BRIDGE_CONNECT_CHECK": str(FAKES / "connect-check"),
            "SLACK_BRIDGE_WORKSPACE_CHECK": str(FAKES / "workspace-check"),
            "SLACK_BRIDGE_SETTLE_SECONDS": "0",
        }
        base.update(env)
        for k in [k for k, v in base.items() if v is None]:
            del base[k]
        proc = subprocess.run([str(self.repo / "scripts" / "setup.sh")], input=answers,
                              env=base, capture_output=True, text=True, check=False,
                              timeout=60)
        self.out = proc.stdout + proc.stderr
        return proc

    def store(self):
        p = self.dir / "store.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def log(self, name):
        p = self.dir / name
        return p.read_text().splitlines() if p.exists() else []

    def slack_calls(self):
        return [line.split("|")[0] for line in self.log("calls.log")]

    def installed(self):
        """An install that setup.sh already finished once."""
        (self.dir / "auth").write_text(ONE)
        (self.repo / ".slack" / "apps.json").write_text(
            json.dumps({"apps": {"T0123": {"app_id": "A0FAKE"}}}))
        (self.dir / "store.json").write_text(json.dumps({
            "captain/slack-bridge.bot-token": BOT,
            "captain/slack-bridge.app-token": APP,
            "captain/slack-bridge.captain-user-id": "U0CAPTAIN"}))
        support = self.home / "Library" / "Application Support" / "slack-bridge"
        support.mkdir(parents=True)
        (support / "workspace").write_text("T0123\n")
        (support / "app-id").write_text("A0FAKE\n")
        (support / "fm-home").write_text(f"{self.fm}\n")

    def assert_no_secrets(self):
        argv = "\n".join(self.log("argv.log") + self.log("calls.log") + self.log("bridge.log")
                         + self.log("install.log"))
        for secret in (BOT, APP):
            self.assertNotIn(secret, self.out)
            self.assertNotIn(secret, argv)

    def test_fresh_install_end_to_end(self):
        # Asked: the Firstmate folder (not found), then whether the test DM
        # arrived (yes); a fresh login needs no workspace question.
        proc = self.run_setup("~/fm\n\n")
        self.assertEqual(proc.returncode, 0, self.out)
        for n in range(1, 8):
            self.assertIn(f"[{n}/7]", self.out)
        self.assertIn("login", self.slack_calls())
        self.assertIn("deploy --team T0123|NO_INSTALL=1", self.log("calls.log"))
        self.assertIn("/slackauthticket", self.out)
        self.assertIn('choose "Create a new app".', self.out)
        self.assertEqual(self.store(), {
            "captain/slack-bridge.bot-token": BOT,
            "captain/slack-bridge.app-token": APP,
            "captain/slack-bridge.captain-user-id": "U0CAPTAIN",
        })
        fm_home = self.home / "Library" / "Application Support" / "slack-bridge" / "fm-home"
        self.assertEqual(fm_home.read_text().strip(), str(self.fm))
        self.assertEqual(self.log("install.log"), ["--deps-only", "--skip-doctor"])
        bridge = self.log("bridge.log")
        self.assertEqual(bridge[0], "doctor")
        self.assertTrue(bridge[1].startswith("say "))
        self.assertEqual(self.log("connect.log"), ["check"])
        self.assertIn("Slack accepts the app-level token", self.out)
        self.assertIn("ping from my phone", self.out)
        self.assert_no_secrets()

    def test_rerun_skips_finished_steps(self):
        self.installed()
        # Firstmate folder (yes), workspace (yes), update the app (no), DM (yes).
        proc = self.run_setup("\n\nn\n\n")
        self.assertEqual(proc.returncode, 0, self.out)
        self.assertIn(f"Found Firstmate at {self.fm}", self.out)
        self.assertNotIn("login", self.slack_calls())
        self.assertFalse([c for c in self.slack_calls() if c.startswith("deploy")])
        self.assertIn("Keeping the app as it is", self.out)
        self.assertEqual(self.log("install.log"), ["--deps-only", "--skip-doctor"])
        self.assert_no_secrets()

    def test_rerun_updates_the_existing_app(self):
        self.installed()
        proc = self.run_setup("\n\n\n\n")
        self.assertEqual(proc.returncode, 0, self.out)
        self.assertEqual(self.slack_calls().count("deploy --team T0123"), 1)
        self.assertIn("pick your existing Firstmate app", self.out)
        self.assertNotIn('choose "Create a new app"', self.out)
        self.assert_no_secrets()

    def test_second_real_rerun_preserves_binding_and_member(self):
        self.assertEqual(self.run_setup("~/fm\n\n").returncode, 0, self.out)
        first_store = self.store()
        self.assertEqual(self.run_setup("\n\nn\n\n").returncode, 0, self.out)
        self.assertEqual(self.store(), first_store)
        self.assertEqual(self.slack_calls().count("login"), 1)
        self.assertEqual(self.slack_calls().count("deploy --team T0123"), 1)
        self.assert_no_secrets()

    def test_cross_workspace_rerun_stops_before_credentials_or_deploy(self):
        self.installed()
        original = self.store()
        (self.dir / "auth").write_text("work\nTeam ID: T0999\nUser ID: U0OTHER")
        proc = self.run_setup("\n\n")
        self.assertEqual(proc.returncode, 1, self.out)
        self.assertIn("already has a bridge for another workspace", self.out)
        self.assertEqual(self.store(), original)
        self.assertFalse([c for c in self.slack_calls() if c.startswith("deploy")])
        self.assertNotIn("--skip-doctor", self.log("install.log"))

    def test_legacy_credentials_are_verified_before_workspace_replacement(self):
        self.installed()
        binding = self.home / "Library/Application Support/slack-bridge/workspace"
        binding.unlink()
        original = self.store()
        proc = self.run_setup("\n\n", FAKE_STORED_TEAM="T0999")
        self.assertEqual(proc.returncode, 1, self.out)
        self.assertIn("already has a bridge for another workspace", self.out)
        self.assertEqual(self.store(), original)
        self.assertNotIn("--skip-doctor", self.log("install.log"))

    def test_unverifiable_legacy_credentials_stop_before_overwrite(self):
        self.installed()
        (self.home / "Library/Application Support/slack-bridge/workspace").unlink()
        original = self.store()
        proc = self.run_setup("\n\n", FAKE_WORKSPACE_ERROR="offline")
        self.assertEqual(proc.returncode, 1, self.out)
        self.assertIn("Could not verify the workspace", self.out)
        self.assertEqual(self.store(), original)

    def test_bound_rerun_keeps_app_token_when_deploy_supplies_none(self):
        self.installed()
        # Firstmate folder (yes), workspace (yes), update the app (yes), DM (yes).
        proc = self.run_setup("\n\n\n\n", FAKE_SLACK_APP=None)
        self.assertEqual(proc.returncode, 0, self.out)
        self.assertEqual(self.slack_calls().count("deploy --team T0123"), 1)
        self.assertNotIn("Paste the xapp- token", self.out)
        self.assertNotIn("app settings", self.slack_calls())
        self.assertEqual(self.store()["captain/slack-bridge.app-token"], APP)
        self.assert_no_secrets()

    def test_new_app_in_bound_workspace_asks_for_its_own_app_token(self):
        self.installed()
        (self.repo / ".slack" / "apps.json").unlink()
        new = "xapp-1-A222-444-new789"
        # Firstmate folder (yes), workspace (yes), open settings (no), paste, DM (yes).
        proc = self.run_setup(f"\n\nn\n{new}\n\n", FAKE_SLACK_APP=None)
        self.assertEqual(proc.returncode, 0, self.out)
        self.assertIn('choose "Create a new app"', self.out)
        self.assertIn("Paste the xapp- token", self.out)
        self.assertEqual(self.store()["captain/slack-bridge.app-token"], new)
        self.assertNotIn(new, self.out)
        self.assert_no_secrets()

    def test_rerun_in_older_clone_does_not_keep_another_apps_token(self):
        # Clone A creates app X; clone B then creates app Y and stores Y's token.
        clone_a = self.repo
        self.assertEqual(self.run_setup("~/fm\n\n", FAKE_SLACK_APP_ID="A0X").returncode, 0, self.out)
        self.repo = self.dir / "clone-b"
        (self.repo / ".slack").mkdir(parents=True)
        shutil.copytree(REPO / "scripts", self.repo / "scripts")
        other = "xapp-1-A0Y-555-other"
        self.assertEqual(self.run_setup("\n\n\n", FAKE_SLACK_APP_ID="A0Y",
                                        FAKE_SLACK_APP=other).returncode, 0, self.out)
        self.assertEqual(self.store()["captain/slack-bridge.app-token"], other)
        # Rerun in clone A, updating app X; Slack supplies no app-level token.
        self.repo = clone_a
        new = "xapp-1-A0X-666-fresh"
        proc = self.run_setup(f"\n\n\nn\n{new}\n\n", FAKE_SLACK_APP_ID="A0X", FAKE_SLACK_APP=None)
        self.assertEqual(proc.returncode, 0, self.out)
        self.assertIn("Paste the xapp- token", self.out)
        self.assertEqual(self.store()["captain/slack-bridge.app-token"], new)
        support = self.home / "Library/Application Support/slack-bridge"
        self.assertEqual((support / "app-id").read_text().strip(), "A0X")
        self.assert_no_secrets()

    def test_unbound_app_token_is_replaced_when_deploy_supplies_none(self):
        self.installed()
        (self.home / "Library/Application Support/slack-bridge/workspace").unlink()
        (self.dir / "store.json").write_text(json.dumps({
            "captain/slack-bridge.app-token": "xapp-1-AOLD-000-stale"}))
        proc = self.run_setup(f"\n\nn\n{APP}\n\n", FAKE_SLACK_APP=None)
        self.assertEqual(proc.returncode, 0, self.out)
        self.assertIn("Paste the xapp- token", self.out)
        self.assertEqual(self.store()["captain/slack-bridge.app-token"], APP)
        self.assert_no_secrets()

    def test_guided_app_token_fallback(self):
        # Slack hands over no app-level token: setup asks for it, hidden,
        # turns down a bot token and stores the real one over stdin.
        proc = self.run_setup(f"~/fm\nn\n{BOT}\n{APP}\n\n", FAKE_SLACK_APP=None)
        self.assertEqual(proc.returncode, 0, self.out)
        self.assertIn("App-Level Tokens", self.out)
        self.assertIn("connections:write", self.out)
        self.assertIn("bot or user token", self.out)
        self.assertIn("setup asks you for one next", self.out)
        self.assertNotIn("security add-generic-password", self.out)
        self.assertEqual(self.store()["captain/slack-bridge.app-token"], APP)
        self.assertNotIn("app settings", self.slack_calls())
        self.assertIn("--skip-doctor", self.log("install.log"))
        self.assert_no_secrets()

    def test_fallback_says_why_the_keychain_refused_the_token(self):
        proc = self.run_setup(f"~/fm\nn\n{APP}\n{APP}\n{APP}\n", FAKE_SLACK_APP=None,
                              FAKE_SECURITY_REFUSE="slack-bridge.app-token")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.out.count("Not saved: could not write Keychain item slack-bridge.app-token"), 3)
        self.assertIn("unlock it in Keychain Access", self.out)
        self.assertIn("No app-level token was saved", self.out)
        self.assert_no_secrets()

    def test_fallback_opens_app_settings(self):
        proc = self.run_setup(f"~/fm\n\n{APP}\n\n", FAKE_SLACK_APP=None)
        self.assertEqual(proc.returncode, 0, self.out)
        self.assertIn("app settings", self.slack_calls())
        self.assert_no_secrets()

    def test_picks_one_of_several_workspaces(self):
        (self.dir / "auth").write_text(TWO)
        proc = self.run_setup("~/fm\n7\n2\n\n")
        self.assertEqual(proc.returncode, 0, self.out)
        self.assertIn("1. myteam (U0CAPTAIN)", self.out)
        self.assertIn("2. other (Team ID: T0999) (U0OTHER)", self.out)
        self.assertIn("Type a number from 1 to 2", self.out)
        # The hook keeps the stored id when more than one account is logged in.
        self.assertEqual(self.store()["captain/slack-bridge.captain-user-id"], "U0OTHER")
        # The app goes to the workspace picked here; the CLI does not ask again.
        self.assertIn("deploy --team T0999", self.slack_calls())
        self.assertNotIn("then your workspace", self.out)

    def test_workspace_without_team_id_stops_before_changing_credentials(self):
        proc = self.run_setup("~/fm\n\n", FAKE_SLACK_LOGIN_AS="myteam\nUser ID: U0CAPTAIN")
        self.assertEqual(proc.returncode, 1, self.out)
        self.assertIn("Could not identify the selected workspace", self.out)
        self.assertEqual(self.store(), {})
        self.assertNotIn("deploy", self.slack_calls())

    def test_rejected_app_token_stops_before_success(self):
        proc = self.run_setup("~/fm\n\n", FAKE_CONNECT_ERROR="Slack answered invalid_auth")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("The bridge cannot connect to Slack: Slack answered invalid_auth.", self.out)
        self.assertIn("messages you send from Slack will not reach the bridge", self.out)
        self.assertIn("connections:write", self.out)
        self.assertNotIn("All set", self.out)
        self.assertEqual(self.log("bridge.log"), ["doctor"])
        self.assert_no_secrets()

    def test_rate_limited_token_check_warns_and_continues(self):
        proc = self.run_setup("~/fm\n\n", FAKE_CONNECT_ERROR="Slack is rate-limiting the check",
                              FAKE_CONNECT_RC="3")
        self.assertEqual(proc.returncode, 0, self.out)
        self.assertIn("Could not verify the app-level token: Slack is rate-limiting", self.out)
        self.assertNotIn("The bridge cannot connect to Slack", self.out)
        self.assertNotIn("Slack accepts the app-level token", self.out)
        self.assertIn("All set", self.out)
        self.assertTrue(self.log("bridge.log")[1].startswith("say "))
        self.assert_no_secrets()

    def test_bridge_restarting_after_start_stops_before_success(self):
        (self.dir / "launchctl.json").write_text(json.dumps({"loaded": True, "pids": [4242, 4250]}))
        proc = self.run_setup("~/fm\n\n")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("The bridge stopped or restarted right after starting", self.out)
        self.assertNotIn("All set", self.out)

    def test_failed_deploy_stops_with_next_step(self):
        proc = self.run_setup("~/fm\n\n", FAKE_SLACK_DEPLOY_RC="1")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("slack deploy stopped before the app was installed (exit 1)", self.out)
        self.assertIn("run scripts/setup.sh again", self.out)
        self.assertEqual(self.log("install.log"), ["--deps-only"])

    def test_firstmate_not_running_is_a_warning(self):
        proc = self.run_setup("~/fm\n\n\n\n", FAKE_DOCTOR_RC="1",
                              FAKE_DOCTOR="FAIL no Firstmate pane found in Herdr session 'default'")
        self.assertEqual(proc.returncode, 0, self.out)
        self.assertIn("Firstmate is not running in Herdr right now", self.out)
        self.assertIn("--skip-doctor", self.log("install.log"))

    def test_doctor_keychain_failure_stops(self):
        proc = self.run_setup("~/fm\n\n", FAKE_DOCTOR_RC="1",
                              FAKE_DOCTOR="FAIL Keychain item 'slack-bridge.app-token' is missing")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("The setup check found a problem", self.out)
        self.assertNotIn("--skip-doctor", self.log("install.log"))

    def test_bad_firstmate_path_is_asked_again(self):
        proc = self.run_setup("/nowhere\n~/fm\n\n\n")
        self.assertEqual(proc.returncode, 0, self.out)
        self.assertIn("/nowhere has no bin/fm-inbox.sh", self.out)

    def test_firstmate_path_gives_up_after_three_tries(self):
        proc = self.run_setup("")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("No Firstmate folder found", self.out)

    def test_missing_slack_cli_declined(self):
        proc = self.run_setup("n\n", SLACK_BRIDGE_SLACK=None, PATH="/usr/bin:/bin")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("The Slack CLI is not installed", self.out)
        self.assertIn("https://api.slack.com/automation/cli/install-mac", self.out)

    def test_missing_slack_cli_installed_from_the_official_installer(self):
        installer = self.dir / "slack-install.sh"
        installer.write_text(f'mkdir -p "$HOME/.slack/bin"\ncp "{FAKES / "slack"}" "$HOME/.slack/bin/slack"\n')
        # Stops at the Firstmate folder (no answers); the CLI was found by then.
        proc = self.run_setup("y\n", SLACK_BRIDGE_SLACK=None, PATH="/usr/bin:/bin",
                              SLACK_BRIDGE_SLACK_INSTALLER=installer.as_uri())
        self.assertIn(f"Slack CLI v4.12.0 ({self.home}/.slack/bin/slack)", self.out)
        self.assertIn("[3/7]", self.out)
        self.assertEqual(proc.returncode, 1)

    def test_old_slack_cli_is_upgraded(self):
        proc = self.run_setup("\n", FAKE_SLACK_VERSION="4.2.0")
        self.assertIn("older than the v4.11", self.out)
        self.assertIn("upgrade", self.slack_calls())


class FmHomeConfigTest(unittest.TestCase):
    def test_saved_fm_home_and_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "fm-home").write_text("/somewhere/firstmate\n")
            env = {"SLACK_BRIDGE_STATE_DIR": tmp}
            self.assertEqual(Config.from_env(env).fm_home, "/somewhere/firstmate")
            env["SLACK_BRIDGE_FM_HOME"] = "/env/fm"
            self.assertEqual(Config.from_env(env).fm_home, "/env/fm")

    def test_default_fm_home_is_under_this_users_home(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("slack_bridge.config.DEFAULT_STATE_DIR", Path(tmp)):
            cfg = Config.from_env({"SLACK_BRIDGE_STATE_DIR": tmp})
        self.assertEqual(cfg.fm_home, str(Path.home() / "Documents" / "firstmate"))

    def test_generic_defaults_and_environment_choices(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("slack_bridge.config.DEFAULT_STATE_DIR", Path(tmp)):
            cfg = Config.from_env({"SLACK_BRIDGE_STATE_DIR": tmp})
            custom = Config.from_env({"SLACK_BRIDGE_STATE_DIR": tmp,
                                      "SLACK_BRIDGE_FM_HOME": "/test/home",
                                      "SLACK_BRIDGE_HERDR_SESSION": "work-session",
                                      "SLACK_BRIDGE_PANE": "test:p1",
                                      "SLACK_BRIDGE_KEEPER": "0"})
        self.assertEqual(cfg.fm_inbox, str(Path.home() / "Documents/firstmate/bin/fm-inbox.sh"))
        self.assertEqual(cfg.herdr_bin, "herdr")
        self.assertEqual(cfg.herdr_session, "default")
        self.assertIsNone(cfg.target_pane)
        self.assertEqual(custom.fm_home, "/test/home")
        self.assertEqual(custom.herdr_session, "work-session")
        self.assertEqual(custom.target_pane, "test:p1")
        self.assertFalse(custom.keeper_enabled)

    def test_other_state_dir_falls_back_to_the_saved_fm_home(self):
        with tempfile.TemporaryDirectory() as default, tempfile.TemporaryDirectory() as dev, \
                mock.patch("slack_bridge.config.DEFAULT_STATE_DIR", Path(default)):
            (Path(default) / "fm-home").write_text("/somewhere/firstmate\n")
            self.assertEqual(Config.from_env({"SLACK_BRIDGE_STATE_DIR": dev}).fm_home,
                             "/somewhere/firstmate")
            (Path(dev) / "fm-home").write_text("/dev/firstmate\n")
            self.assertEqual(Config.from_env({"SLACK_BRIDGE_STATE_DIR": dev}).fm_home,
                             "/dev/firstmate")


if __name__ == "__main__":
    unittest.main()
