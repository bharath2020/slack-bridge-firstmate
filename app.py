"""Entry point for `slack run` (the Slack CLI's start hook runs app.py).

For a live drill only: `slack run` installs a separate "(local)" dev app and
hands this process its tokens in SLACK_BOT_TOKEN / SLACK_APP_TOKEN. The
always-on bridge is the LaunchAgent (`slack deploy`, see SETUP.md), so this
entry point runs with the keeper, the load alarm and the fleet guard off and
its own state directory, never touches the Firstmate pane, and refuses
`restart bridge` (it is not the LaunchAgent).
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ["SLACK_BRIDGE_TOKENS_FROM_ENV"] = "1"
os.environ.setdefault("SLACK_BRIDGE_KEEPER", "0")
os.environ.setdefault("SLACK_BRIDGE_LOAD_ALARM", "0")
os.environ.setdefault("SLACK_BRIDGE_GUARD", "0")
os.environ["SLACK_BRIDGE_LAUNCHD_LABEL"] = ""
os.environ.setdefault(
    "SLACK_BRIDGE_STATE_DIR",
    str(Path.home() / "Library" / "Application Support" / "slack-bridge" / "dev"),
)

from slack_bridge.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["run"]))
