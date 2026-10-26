"""Configuration: fixed Keychain service names plus env overrides for paths.

Secrets live only in the macOS login Keychain and are read at start with
``security find-generic-password -w``. Nothing here ever logs or prints them.
"""

from __future__ import annotations

import getpass
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

KEYCHAIN_BOT_TOKEN = "slack-bridge.bot-token"  # xoxb-...
KEYCHAIN_APP_TOKEN = "slack-bridge.app-token"  # xapp-...
KEYCHAIN_CAPTAIN_ID = "slack-bridge.captain-user-id"  # U... or Enterprise Grid W...

DEFAULT_FM_HOME = str(Path.home() / "Documents" / "firstmate")
# scripts/setup.sh saves the Firstmate home it found or was told, one path on
# one line, as this file in the state dir; SLACK_BRIDGE_FM_HOME still wins.
# The default state dir's copy is the fallback, so the `slack run` drill
# (app.py, its own state dir) uses the same Firstmate home.
FM_HOME_FILE = "fm-home"
DEFAULT_STATE_DIR = Path.home() / "Library" / "Application Support" / "slack-bridge"
DEFAULT_LOG_FILE = Path.home() / "Library" / "Logs" / "slack-bridge" / "bridge.log"


class ConfigError(RuntimeError):
    pass


def keychain_read(service: str, account: str | None = None) -> str:
    """Return a generic password from the login Keychain, or raise ConfigError.

    The value is captured from stdout and never echoed; error output from
    ``security`` names only the service.
    """
    account = account or getpass.getuser()
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"cannot read Keychain item {service!r}: {exc.__class__.__name__}")
    value = proc.stdout.strip()
    if proc.returncode != 0 or not value:
        raise ConfigError(
            f"Keychain item {service!r} (account {account!r}) is missing; see SETUP.md"
        )
    return value


@dataclass
class Config:
    fm_home: str = DEFAULT_FM_HOME
    state_dir: Path = DEFAULT_STATE_DIR
    log_file: Path = DEFAULT_LOG_FILE
    herdr_bin: str = "herdr"
    herdr_session: str = "default"
    # Explicit pane id; when set, discovery is skipped entirely. Tests always
    # set this (with a fake herdr) so they can never resolve the live pane.
    target_pane: str | None = None
    agent_cmd: str = "claude"
    receipts_poll_seconds: float = 5.0
    keeper_poll_seconds: float = 60.0
    fresh_wait_seconds: float = 300.0
    # `restart` suggests `restart force` once Firstmate has shown the same
    # busy status for this long.
    stuck_seconds: float = 600.0
    restart_limit: int = 3
    restart_window_seconds: int = 3600
    keeper_enabled: bool = True
    # `shot`: the Playwright CLI, driving the installed Chrome.
    playwright_bin: str = "playwright"
    # Hand-off of a Slack message to fm-inbox.sh note, and its retry loop.
    handoff_timeout_seconds: float = 180.0
    handoff_poll_seconds: float = 10.0
    # Load alarm: the 5-min load average above the threshold (absolute, or
    # load_factor x cores when unset) for load_sustain_seconds.
    load_alarm_enabled: bool = True
    load_threshold: float | None = None
    load_factor: float = 2.0
    load_sustain_seconds: float = 300.0
    load_poll_seconds: float = 30.0
    load_realert_seconds: float = 1800.0
    # Fleet guard: background priority for heavy processes and their children
    # from the moment the 1-minute load average crosses the threshold.
    guard_enabled: bool = True
    ps_bin: str = "ps"
    sysctl_bin: str = "sysctl"
    taskpolicy_bin: str = "taskpolicy"
    launchctl_bin: str = "launchctl"
    # `shed` and `restart` suggestions from one `claude -p` call (`codex exec`
    # when claude is missing); off, or on any failure, the rules decide.
    suggest_enabled: bool = True
    suggest_cli: str = "claude"
    suggest_fallback_cli: str = "codex"
    suggest_timeout_seconds: float = 25.0
    launchd_label: str = "com.firstmate.slack-bridge"

    @property
    def fm_inbox(self) -> str:
        return os.path.join(self.fm_home, "bin", "fm-inbox.sh")

    @property
    def slack_team_id(self) -> str | None:
        """Workspace grant selected by setup for an organization bot token."""
        try:
            team = (self.state_dir / "org-workspace").read_text().strip()
        except FileNotFoundError:
            return None
        if not re.fullmatch(r"T[A-Z0-9]+", team):
            raise ConfigError("invalid org-workspace binding; rerun scripts/setup.sh")
        return team

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = dict(os.environ if env is None else env)
        cfg = cls()
        if "SLACK_BRIDGE_STATE_DIR" in env:
            cfg.state_dir = Path(env["SLACK_BRIDGE_STATE_DIR"])
        cfg.fm_home = env.get("SLACK_BRIDGE_FM_HOME") or saved_fm_home(cfg.state_dir) or saved_fm_home(DEFAULT_STATE_DIR) or cfg.fm_home
        if "SLACK_BRIDGE_LOG_FILE" in env:
            cfg.log_file = Path(env["SLACK_BRIDGE_LOG_FILE"])
        cfg.herdr_bin = env.get("SLACK_BRIDGE_HERDR", cfg.herdr_bin)
        cfg.herdr_session = env.get("SLACK_BRIDGE_HERDR_SESSION", cfg.herdr_session)
        cfg.target_pane = env.get("SLACK_BRIDGE_PANE") or None
        cfg.agent_cmd = env.get("SLACK_BRIDGE_AGENT_CMD", cfg.agent_cmd)
        cfg.keeper_enabled = env.get("SLACK_BRIDGE_KEEPER", "1") != "0"
        cfg.playwright_bin = env.get("SLACK_BRIDGE_PLAYWRIGHT", cfg.playwright_bin)
        cfg.load_alarm_enabled = env.get("SLACK_BRIDGE_LOAD_ALARM", "1") != "0"
        cfg.guard_enabled = env.get("SLACK_BRIDGE_GUARD", "1") != "0"
        cfg.launchd_label = env.get("SLACK_BRIDGE_LAUNCHD_LABEL", cfg.launchd_label)
        cfg.suggest_enabled = env.get("SLACK_BRIDGE_SUGGEST", "1") != "0"
        cfg.suggest_cli = env.get("SLACK_BRIDGE_SUGGEST_CLI", cfg.suggest_cli)
        cfg.suggest_fallback_cli = env.get("SLACK_BRIDGE_SUGGEST_CODEX", cfg.suggest_fallback_cli)
        for name, attr, conv in (
            ("SLACK_BRIDGE_RECEIPTS_POLL", "receipts_poll_seconds", float),
            ("SLACK_BRIDGE_KEEPER_POLL", "keeper_poll_seconds", float),
            ("SLACK_BRIDGE_FRESH_WAIT", "fresh_wait_seconds", float),
            ("SLACK_BRIDGE_STUCK_AFTER", "stuck_seconds", float),
            ("SLACK_BRIDGE_RESTART_LIMIT", "restart_limit", int),
            ("SLACK_BRIDGE_HANDOFF_TIMEOUT", "handoff_timeout_seconds", float),
            ("SLACK_BRIDGE_LOAD_THRESHOLD", "load_threshold", float),
            ("SLACK_BRIDGE_LOAD_FACTOR", "load_factor", float),
            ("SLACK_BRIDGE_LOAD_SUSTAIN", "load_sustain_seconds", float),
            ("SLACK_BRIDGE_LOAD_POLL", "load_poll_seconds", float),
            ("SLACK_BRIDGE_LOAD_REALERT", "load_realert_seconds", float),
            ("SLACK_BRIDGE_SUGGEST_TIMEOUT", "suggest_timeout_seconds", float),
        ):
            if name in env:
                setattr(cfg, attr, conv(env[name]))
        return cfg


def saved_fm_home(state_dir: Path) -> str | None:
    try:
        saved = (state_dir / FM_HOME_FILE).read_text().strip()
    except OSError:
        return None
    return saved or None


@dataclass
class Secrets:
    bot_token: str
    app_token: str | None
    captain_id: str

    def __repr__(self) -> str:  # never let a token reach a log line
        return f"Secrets(captain_id={self.captain_id!r}, tokens=<redacted>)"


def load_secrets(need_app_token: bool = True, env: dict[str, str] | None = None) -> Secrets:
    env = os.environ if env is None else env
    captain = env.get("SLACK_BRIDGE_CAPTAIN_ID") or keychain_read(KEYCHAIN_CAPTAIN_ID)
    # Under `slack run` (app.py) the Slack CLI hands the dev app's tokens in
    # the environment; the LaunchAgent always reads the Keychain.
    from_env = env.get("SLACK_BRIDGE_TOKENS_FROM_ENV") == "1"
    bot = (from_env and env.get("SLACK_BOT_TOKEN")) or keychain_read(KEYCHAIN_BOT_TOKEN)
    app = None
    if need_app_token:
        app = (from_env and env.get("SLACK_APP_TOKEN")) or keychain_read(KEYCHAIN_APP_TOKEN)
    if not bot.startswith("xoxb-"):
        raise ConfigError(f"{KEYCHAIN_BOT_TOKEN} does not look like a bot token (xoxb-)")
    if app is not None and not app.startswith("xapp-"):
        raise ConfigError(f"{KEYCHAIN_APP_TOKEN} does not look like an app token (xapp-)")
    return Secrets(bot_token=bot, app_token=app, captain_id=captain.strip())
