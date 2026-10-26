# Slack bridge setup

The bridge connects one Slack workspace to one Firstmate home on your Mac. You do the Slack approvals yourself; the wizard explains each prompt. Allow about ten minutes, longer if a workspace administrator must approve the app.

## Before you start

- macOS, a logged-in desktop session, and an unlocked login Keychain. No sudo is needed.
- Python 3.9 or newer (`python3 --version`), with venv and pip. Homebrew users can run `brew install python`.
- Git (`git --version`); if missing, install Apple's command line tools with `xcode-select --install`.
- Herdr installed and on PATH, plus an existing Firstmate home containing executable `bin/fm-inbox.sh`. Start Firstmate in Herdr for the keeper checks. Setup does not install Firstmate or Herdr.
- Slack open in the intended workspace, permission to create/install apps, and internet access to GitHub, Python packages, and Slack. Workspaces that restrict apps need administrator approval. The app uses Socket Mode, so it needs no public HTTP endpoint.

This installs one bridge per macOS login account, using fixed Keychain names and a LaunchAgent label. Use a different Mac or macOS account to keep personal and work workspaces separate. Messages and attachments delivered to Firstmate become local task data; only connect a work workspace you are authorized to use with that home and its agents.

## Install with one command

After the repository is published:

```sh
curl -fsSL https://raw.githubusercontent.com/bharath2020/slack-bridge-firstmate/main/scripts/install.sh | bash
```

Run it in an interactive Terminal window. It reads wizard answers from the terminal even when the script arrives through a pipe. The repository is currently private; the unauthenticated URL will not work until publication is approved. Until then, use an authenticated clone:

```sh
git clone https://github.com/bharath2020/slack-bridge-firstmate.git
```

Then run `./scripts/install.sh` inside that clone. Review the downloaded script before running it if preferred. Both entry points start the same wizard; `./scripts/setup.sh` remains available to rerun just onboarding.

The remote entry point clones to `~/.local/share/slack-bridge`. Rerunning that command updates that copy by fast-forward only, then starts onboarding. It refuses a different origin, non-repository directory, local changes, or diverged history rather than overwriting work. A clone invocation uses your current checkout without fetching.

To choose a different managed location, pass the variable to bash, not curl:

```sh
curl -fsSL https://raw.githubusercontent.com/bharath2020/slack-bridge-firstmate/main/scripts/install.sh | SLACK_BRIDGE_INSTALL_DIR="$HOME/Applications/slack-bridge" bash
```

`SLACK_BRIDGE_REPO_URL` overrides the repository for an approved fork; its `main` branch is installed. Download the installer from that fork as well. `PYTHON` selects a Python executable. Keep the installed repository in place: the background service runs from it.

## What the wizard asks

Enter accepts the default in brackets. Tokens are never echoed or put in command arguments; do not paste tokens into chat, issues, or shell commands.

1. **Python and Slack CLI.** Checks Python 3.9+ and Herdr on PATH. Finds the official Slack CLI (v4.11+), offers to install it from Slack if absent, or upgrade an older version. Installation and upgrades require your confirmation.
2. **Python packages.** Creates or reuses `.venv` in the installed repository and refreshes `requirements.txt`. This environment is used by Slack CLI hooks and the service.
3. **Firstmate folder.** Looks for a previous choice or common home locations, then asks you to confirm or enter the full path. `~` is accepted. The folder must contain executable `bin/fm-inbox.sh`. The selection is saved under `~/Library/Application Support/slack-bridge/fm-home`. You can use any home path; `~/Documents/firstmate` is only a fallback.
4. **Slack workspace and member.** Runs `slack login` when needed: paste the `/slackauthticket …` command into Slack, approve it, and enter Slack's challenge code in Terminal. Select the intended workspace if several are logged in. The selected member ID is stored in the Keychain; the bridge accepts commands only from that member. A saved workspace binding prevents accidental workspace switches. For older installs without that binding, setup verifies the bot token's workspace with Slack before changing credentials.
5. **Create or update the app.** Runs `slack deploy` with the selected workspace and [`manifest.json`](manifest.json). On a fresh install choose **Create a new app**; on reruns choose the existing **Firstmate** app. The CLI retains its app binding in ignored `.slack/apps.json`. Approve Slack's listed permissions. Bot and app-level tokens go into the login Keychain. If Slack supplies no app-level token, setup opens the app settings if you agree and says exactly what to do: **Basic Information → App-Level Tokens → Generate Token and Scopes**, add **connections:write**, generate, copy, then **paste the xapp- token** into the hidden terminal prompt. When Slack supplies none while updating this clone's existing app, setup keeps the stored app-level token if the saved workspace matches and the token was recorded for this same app. It asks for a new one when none is stored, the workspace cannot be confirmed, the token belongs to another app or was never recorded, or this clone creates a new app.
6. **Background service.** Runs `bin/slack-bridge doctor`. Missing Keychain items, packages, or the inbox helper stop setup. If the Firstmate pane is unavailable, setup asks before continuing. It writes `~/Library/LaunchAgents/com.firstmate.slack-bridge.plist` and reloads only that bridge service. It runs at login and restarts after failure.
7. **Live check.** Checks that the service runs, verifies the app-level token against Slack, sends a test DM to your selected account, and checks that the service remains running. A rate limit produces a warning instead of claiming inbound delivery was verified. Confirm that the DM arrived in **Apps → Firstmate** in the intended workspace.

Slack's [CLI hooks](https://docs.slack.dev/tools/slack-cli/reference/hooks/) perform manifest deployment and pass tokens to the hook. Slack's [Socket Mode guide](https://docs.slack.dev/apis/events-api/using-socket-mode/) explains the separate app-level token. The wizard cannot approve workspace policies, login challenges, or token creation for you.

## Verify the round trip

After setup, in the DM with **Firstmate**:

- Send `status`. The inbox status should arrive in a thread without waking Firstmate.
- Send `ping from Slack`. It gets 👀 when queued, then Firstmate's reply should arrive in the same thread. This checks inbound routing and replies, beyond the wizard's outbound test DM.

From the installed repository, `bin/slack-bridge doctor` checks configuration and `bin/slack-bridge say "hello from the Mac"` sends another DM. Logs are in `~/Library/Logs/slack-bridge/bridge.log` and `launchd.err.log`. The bridge's own logging omits message bodies and tokens; local state and inbound attachments are private task data, so keep them out of source control. Inbound pictures expire after seven days.

For private project channels, try `bin/slack-bridge channel create example-project example-project`. Only the configured member's messages are accepted. See [README.md](README.md) for topic threads, screenshots, and commands.

## Update or rerun

Rerun the remote install command to update a managed install and restart the wizard. Your venv, chosen home, Slack CLI app binding, and Keychain items are reused. Confirm the saved home/workspace, and choose the existing app when updating. You may decline the manifest update on an already bound install with tokens present. After a manifest change, accept the update and approve any newly requested scopes. If Slack asks which app, do not create a second one.

For a manually cloned checkout, run `git pull --ff-only`, then `./scripts/install.sh`. Resolve local changes or divergence yourself before updating; the installer never resets or stashes them. To refresh only dependencies use `./scripts/install-service.sh --deps-only`; `--no-load` renders the service without loading it. A plain `slack deploy` from the repository updates tokens and reloads the bridge too.

## Troubleshooting

| Setup says | Next step |
| --- | --- |
| macOS only / interactive terminal required | Run from Terminal in a macOS login session, not a noninteractive job |
| Python missing / too old | Install Python 3.9+ (`brew install python`), or set `PYTHON` to its executable |
| Git or Herdr missing | Install the prerequisite and open a new terminal so PATH updates |
| Existing directory / different origin or branch / local changes / fast-forward refused | Choose an empty absolute `SLACK_BRIDGE_INSTALL_DIR`, or resolve changes in the managed checkout; do not delete `.slack/apps.json` to fix an update |
| Slack CLI absent / install failed / no version | Accept the installer or follow [Slack's CLI install guide](https://docs.slack.dev/tools/slack-cli/guides/installing-the-slack-cli-for-mac-and-linux/); check connectivity and PATH |
| Slack CLI too old | Run `slack upgrade`, then rerun |
| Python packages failed | Read pip's error, check internet access and Python's venv support, then rerun |
| No Firstmate folder found | Enter the folder containing executable `bin/fm-inbox.sh`; setup retries three times |
| Slack login failed / no account / workspace unidentified | Run `slack login` and `slack auth list`; use a current CLI, paste the auth ticket promptly, and complete the Slack approval |
| Cannot verify existing credentials | Check network access and the old app; for revoked credentials, follow the replacement procedure below |
| Already bound to another workspace | Use another Mac/account, or follow the replacement procedure below |
| Could not save member ID / no app-level token saved | Unlock the login Keychain in Keychain Access; paste an app-level `xapp-` token, not a bot/user token; setup retries three times |
| Deploy stopped / failed after storing tokens | Read Slack's error; complete login or ask your workspace admin to approve the app, then rerun |
| Doctor FAIL for Keychain / fm-inbox / slack_bolt | Rerun and update the app / correct the home path / reinstall packages, respectively |
| Firstmate pane unavailable | Start the expected Firstmate pane in Herdr, or explicitly continue with inbox routing; see README configuration for session and pane selection |
| Could not install service / bootstrap failed | Read launchctl's error; check the login session and generated plist, then rerun |
| Service not running / restarted during check | Read `launchd.err.log` and `bridge.log`, fix the reported cause, then rerun |
| Cannot connect to Slack / invalid_auth / missing scope | Check network access; regenerate the app-level token with `connections:write` in app settings and rerun setup, accepting the app update and pasting the new token when prompted |
| Slack rate-limits the connection check | Wait a minute and rerun; this run did not verify inbound delivery |
| Test DM failed / did not arrive | Confirm Slack is on the chosen workspace, look under Apps → Firstmate, rerun and update the existing app; inspect logs if it persists |
| A project channel or attachment reports missing_scope | Rerun setup and accept the manifest update; the manifest requests private-channel and file scopes |

For a work workspace's approval review: the manifest requests DM history/read/write, chat/reactions, private-channel history/read/write/invites, and file read/write scopes. There are no public-channel scopes. The bridge filters incoming events to your selected member and bot private channels; it does not fetch channel history. Review the manifest with your administrator.

## Uninstall or replace a workspace

From the installed repository:

```sh
./scripts/uninstall.sh          # remove the LaunchAgent; keep data and venv
./scripts/uninstall.sh --purge  # also remove local bridge state, logs, and venv
```

Neither command deletes Keychain items, the Slack app, or the checkout. To revoke the app, run `slack app delete` from that checkout and select the correct app/workspace (this is destructive). Delete the three local credentials separately:

```sh
security delete-generic-password -a "$USER" -s slack-bridge.bot-token
security delete-generic-password -a "$USER" -s slack-bridge.app-token
security delete-generic-password -a "$USER" -s slack-bridge.captain-user-id
```

To replace rather than run alongside another workspace, first stop and purge the old bridge, revoke the old Slack app and remove its Keychain items as above. Preserve any task data you need before purge. Then use a fresh clone/install directory with no old `.slack/apps.json` binding and run the installer. Existing installs from before workspace bindings were saved must update their existing app on the first rerun. Never reuse an old app-level token for a new app. No uninstall or workspace switch is performed automatically.
