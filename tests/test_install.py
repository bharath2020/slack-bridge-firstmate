"""scripts/install.sh and install-service.sh, against local fixtures and fakes only."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from .helpers import FAKES

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "scripts" / "install.sh"
SERVICE = REPO / "scripts" / "install-service.sh"
TARGET = f"gui/{os.getuid()}/com.firstmate.slack-bridge"


class ReloadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sys = Path(self._tmp.name)
        self.plist = self.sys / "agent.plist"

    def tearDown(self):
        self._tmp.cleanup()

    def agent(self, **state):
        (self.sys / "launchctl.json").write_text(json.dumps(state))

    def reload(self):
        # Sourcing install-service.sh only defines reload_agent; nothing is installed.
        env = {"PATH": os.environ["PATH"], "HOME": str(self.sys),
               "FAKE_SYS_DIR": str(self.sys),
               "SLACK_BRIDGE_LAUNCHCTL": str(FAKES / "launchctl")}
        return subprocess.run(["bash", "-c", 'source "$1" && reload_agent "$2"', "bash",
                               str(SERVICE), str(self.plist)],
                              env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, check=False, timeout=30)

    def argv(self):
        log = self.sys / "calls.log"
        return [json.loads(line)[1:] for line in log.read_text().splitlines()]

    def calls(self):
        return [argv[0] for argv in self.argv()]

    def loaded(self):
        return json.loads((self.sys / "launchctl.json").read_text())["loaded"]

    def test_waits_for_old_instance_to_unload_before_bootstrap(self):
        # The 2026-10-24 failure: bootstrap straight after bootout hits the
        # still-unloading old instance and fails with error 5.
        self.agent(loaded=True, unload_ticks=3)
        proc = self.reload()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.calls(), ["bootout", "print", "print", "print", "print",
                                        "bootstrap", "kickstart"])
        self.assertEqual(self.argv()[1], ["print", TARGET])
        self.assertEqual(self.argv()[5], ["bootstrap", f"gui/{os.getuid()}", str(self.plist)])
        self.assertNotIn("Bootstrap failed", proc.stderr)
        self.assertTrue(self.loaded())

    def test_waits_past_a_slow_unload(self):
        # An old instance slow to exit (launchd allows 20 s before SIGKILL)
        # must not exhaust the bootstrap retries.
        self.agent(loaded=True, unload_ticks=55)
        proc = self.reload()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.calls().count("print"), 56)
        self.assertEqual(self.calls()[-2:], ["bootstrap", "kickstart"])
        self.assertTrue(self.loaded())

    def test_retries_a_failed_bootstrap(self):
        self.agent(loaded=True, bootstrap_failures=2)
        proc = self.reload()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.calls().count("bootstrap"), 3)
        self.assertEqual(self.calls()[-1], "kickstart")
        self.assertTrue(self.loaded())

    def test_fresh_install_bootstraps_without_waiting(self):
        self.agent(loaded=False)
        proc = self.reload()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.calls(), ["bootout", "print", "bootstrap", "kickstart"])

    def test_gives_up_with_a_clear_message(self):
        self.agent(loaded=True, bootstrap_failures=99)
        proc = self.reload()
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.calls().count("bootstrap"), 3)
        self.assertNotIn("kickstart", self.calls())
        self.assertIn("could not load com.firstmate.slack-bridge", proc.stderr)
        self.assertIn(f"launchctl bootstrap gui/{os.getuid()}", proc.stderr)
        self.assertFalse(self.loaded())




class BootstrapTest(unittest.TestCase):
    """Run the public installer with local Git fixtures and a fake wizard.

    A pseudo-terminal models curl | bash's human terminal. No real service,
    Keychain, Slack API, package install, or Herdr command can be reached.
    """

    def setUp(self):
        import shutil
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.origin = self.root / "origin"
        self.origin.mkdir()
        self.target = self.root / "managed copy & data"
        self.download = self.root / "download" / "install.sh"
        self.download.parent.mkdir()
        shutil.copy(INSTALL, self.download)
        self.git(self.origin, "init", "-q", "-b", "main")
        self.git(self.origin, "config", "user.email", "fixture@example.invalid")
        self.git(self.origin, "config", "user.name", "Test Fixture")
        (self.origin / "scripts").mkdir()
        setup = self.origin / "scripts/setup.sh"
        # Like `slack deploy`, add a project_id to the tracked config if it lacks one.
        setup.write_text(r'''#!/bin/bash
printf "wizard\n" >> "$HOME/wizard.log"
"$PYTHON" - "$(dirname "$0")/../.slack/config.json" <<'EOF'
import json, sys, uuid
path = sys.argv[1]
with open(path) as f:
    config = json.load(f)
if "project_id" not in config:
    config["project_id"] = str(uuid.uuid4())
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
EOF
''')
        setup.chmod(0o755)
        (self.origin / "requirements.txt").touch()
        (self.origin / ".slack").mkdir()
        shutil.copy(REPO / ".slack/config.json", self.origin / ".slack/config.json")
        (self.origin / ".gitignore").write_text(".venv/\n.slack/apps.json\n")
        (self.origin / "version").write_text("one")
        self.commit("initial")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, repo, *args):
        return subprocess.check_output(["git", "-C", str(repo), *args],
                                       stderr=subprocess.STDOUT, text=True)

    def commit(self, message):
        self.git(self.origin, "add", ".")
        self.git(self.origin, "-c", "commit.gpgsign=false", "commit", "-qm", message)

    def install(self, local=False, piped=False, cwd=None):
        import pty
        import sys
        master, slave = pty.openpty()
        env = {"PATH": os.environ["PATH"], "HOME": str(self.root), "USER": "fixture",
               "PYTHON": sys.executable, "SLACK_BRIDGE_HERDR": str(FAKES / "herdr"),
               "SLACK_BRIDGE_REPO_URL": str(self.origin),
               "SLACK_BRIDGE_INSTALL_DIR": str(self.target)}
        script = self.target / "scripts/install.sh" if local else self.download
        try:
            if piped:
                import fcntl
                import termios
                def terminal():
                    os.setsid()
                    fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
                return subprocess.run(["bash"], input=script.read_text(), pass_fds=(slave,),
                                      preexec_fn=terminal, capture_output=True, text=True,
                                      env=env, cwd=cwd, timeout=30, check=False)
            return subprocess.run(["bash", str(script)], stdin=slave, capture_output=True,
                                  text=True, env=env, timeout=30, check=False)
        finally:
            os.close(master)
            os.close(slave)

    def test_remote_install_twice_preserves_state_and_fast_forwards(self):
        first = self.install(piped=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        # The wizard's Slack CLI step left the tracked .slack/config.json untouched.
        self.assertEqual(self.git(self.target, "status", "--porcelain"), "")
        (self.target / ".venv").mkdir()
        (self.target / ".venv/retained").write_text("keep")
        (self.target / ".slack/apps.json").write_text("private app binding")
        (self.origin / "version").write_text("two")
        self.commit("update")
        second = self.install()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual((self.target / "version").read_text(), "two")
        self.assertEqual((self.target / ".venv/retained").read_text(), "keep")
        self.assertEqual((self.target / ".slack/apps.json").read_text(), "private app binding")
        self.assertEqual((self.root / "wizard.log").read_text().splitlines(), ["wizard", "wizard"])

    def test_piped_install_from_clone_subdirectory_uses_managed_copy(self):
        other = self.root / "other clone"
        (other / "scripts").mkdir(parents=True)
        (other / "docs").mkdir()
        (other / "requirements.txt").touch()
        wrong = other / "scripts/setup.sh"
        wrong.write_text('#!/bin/bash\nprintf "other\\n" >> "$HOME/wizard.log"\n')
        wrong.chmod(0o755)
        result = self.install(piped=True, cwd=other / "docs")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.target / "version").read_text(), "one")
        self.assertEqual((self.root / "wizard.log").read_text().splitlines(), ["wizard"])

    def test_refuses_dirty_checkout_without_overwriting(self):
        self.assertEqual(self.install().returncode, 0)
        (self.target / "version").write_text("local work")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Local changes", result.stderr)
        self.assertEqual((self.target / "version").read_text(), "local work")
        self.assertEqual((self.root / "wizard.log").read_text().splitlines(), ["wizard"])

    def test_refuses_non_repository_and_other_origin(self):
        self.target.mkdir()
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a repository", result.stderr)
        self.target.rmdir()
        self.assertEqual(self.install().returncode, 0)
        self.git(self.target, "remote", "set-url", "origin", str(self.root / "unrelated"))
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different origin", result.stderr)

    def test_clone_entry_uses_checkout_without_fetch(self):
        import shutil
        self.assertEqual(self.install().returncode, 0)
        shutil.copy(INSTALL, self.target / "scripts/install.sh")
        # This untracked source copy is intentionally used as a local checkout.
        self.git(self.target, "remote", "set-url", "origin", str(self.root / "unreachable"))
        result = self.install(local=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "wizard.log").read_text().splitlines(), ["wizard", "wizard"])


class ServiceInstallTest(unittest.TestCase):
    def test_repeat_install_reuses_venv_and_renders_valid_plist_for_special_paths(self):
        import plistlib
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            repo = root / "repo & tools"
            home = root / "home & data"
            (repo / "scripts").mkdir(parents=True)
            (repo / "bin").mkdir()
            shutil.copy(REPO / "scripts/install-service.sh", repo / "scripts/install-service.sh")
            shutil.copytree(REPO / "launchd", repo / "launchd")
            (repo / "requirements.txt").touch()
            shutil.copy(FAKES / "slack-bridge", repo / "bin/slack-bridge")
            python = root / "python"
            python.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$FAKE_PYTHON_LOG"
if [ "$1 $2" = "-m venv" ]; then
  mkdir -p "$3/bin"
  cp "$0" "$3/bin/python3"
fi
''')
            python.chmod(0o755)
            env = {"HOME": str(home), "PATH": os.environ["PATH"], "PYTHON": str(python),
                   "FAKE_PYTHON_LOG": str(root / "python.log"), "FAKE_SYS_DIR": str(root),
                   "FAKE_SLACK_DIR": str(root),
                   "SLACK_BRIDGE_LAUNCHCTL": str(FAKES / "launchctl")}
            command = [str(repo / "scripts/install-service.sh"), "--no-load"]
            for _ in range(2):
                result = subprocess.run(command, env=env, text=True, capture_output=True,
                                        timeout=30, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            calls = (root / "python.log").read_text().splitlines()
            self.assertEqual(sum(c.startswith("-m venv ") for c in calls), 1)
            plist = home / "Library/LaunchAgents/com.firstmate.slack-bridge.plist"
            data = plistlib.loads(plist.read_bytes())
            self.assertEqual(data["ProgramArguments"], [str(repo / "bin/slack-bridge"), "run"])
            self.assertEqual(data["WorkingDirectory"], str(repo))
            self.assertEqual(data["StandardErrorPath"], str(home / "Library/Logs/slack-bridge/launchd.err.log"))
            self.assertFalse((root / "calls.log").exists())


if __name__ == "__main__":
    unittest.main()
