#!/usr/bin/env bash
# slack-deploy-hook.sh - the Slack CLI "deploy" hook (.slack/hooks.json).
#
# `slack deploy` creates or updates the app from manifest.json, installs it
# to the chosen workspace, and then runs this script with the app's tokens in
# SLACK_BOT_TOKEN (xoxb-) and SLACK_APP_TOKEN (xapp-). This script:
#   1. stores both tokens in the login Keychain under the bridge's fixed
#      service names, passing them to `security` on stdin, never argv;
#   2. stores the captain's Slack member id (the account `slack login` used);
#   3. runs scripts/install-service.sh to set up the venv and (re)load the LaunchAgent.
# It never prints a token. scripts/setup.sh runs `slack deploy` as one of its
# steps; a plain `slack deploy` keeps working on its own for existing installs.
#
# Overrides (tests and drills): SLACK_BRIDGE_SECURITY (security binary),
# SLACK_BRIDGE_SLACK (slack binary), SLACK_BRIDGE_CAPTAIN_ID,
# SLACK_BRIDGE_NO_INSTALL=1 (skip install-service.sh), SLACK_BRIDGE_SETUP=1 (run by
# setup.sh, which guides the missing app-level token itself),
# SLACK_BRIDGE_KEEP_APP_TOKEN=1 (setup verified the stored token belongs to this clone's app).
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
slack_cli=${SLACK_BRIDGE_SLACK:-slack}
# setup.sh reads this to tell how far the hook got: tokens-stored or
# no-app-token. It never holds a token.
marker="$HOME/Library/Application Support/slack-bridge/last-deploy"

say() { printf 'slack-bridge: %s\n' "$*"; }
mark() { mkdir -p "$(dirname "$marker")" && printf '%s\n' "$1" >"$marker"; }

# shellcheck source=scripts/keychain.sh
. "$repo/scripts/keychain.sh"

bot=${SLACK_BOT_TOKEN:-}
app=${SLACK_APP_TOKEN:-}

case "$bot" in
  xoxb-*) keychain_store slack-bridge.bot-token "$bot" ;;
  *) say "the Slack CLI did not provide a bot token (SLACK_BOT_TOKEN); is the app installed?"; exit 1 ;;
esac

case "$app" in
  xapp-*)
    keychain_store slack-bridge.app-token "$app"
    rm -f "$(dirname "$marker")/app-id" ;;
  *)
    if { [ "${SLACK_BRIDGE_SETUP:-0}" != 1 ] || [ "${SLACK_BRIDGE_KEEP_APP_TOKEN:-0}" = 1 ]; } &&
       keychain_has slack-bridge.app-token; then
      say "no app-level token from the Slack CLI; keeping the one already in the Keychain"
    elif [ "${SLACK_BRIDGE_SETUP:-0}" = 1 ]; then
      mark no-app-token
      say "the Slack CLI did not provide an app-level token; setup asks you for one next."
      exit 1
    else
      mark no-app-token
      say "the Slack CLI did not provide an app-level token (SLACK_APP_TOKEN)."
      say "Create one: run 'slack app settings', open Basic Information > App-Level Tokens,"
      say "generate a token with scope connections:write, then run:"
      say "  security add-generic-password -U -a \"\$USER\" -s slack-bridge.app-token -w"
      say "and run 'slack deploy' (or scripts/install-service.sh) again; scripts/setup.sh walks you through it."
      exit 1
    fi
    ;;
esac

# The captain is the account that ran `slack login`. With exactly one
# authorized account, `slack auth list` names its member id; otherwise keep
# the stored one or ask.
captain=${SLACK_BRIDGE_CAPTAIN_ID:-}
if [ -z "$captain" ]; then
  ids=$("$slack_cli" auth list --no-color --skip-update 2>/dev/null |
        sed -n 's/^.*User ID: *\(U[A-Z0-9]*\).*$/\1/p' | sort -u || true)
  if [ -n "$ids" ] && [ "$(printf '%s\n' "$ids" | wc -l | tr -d ' ')" = 1 ]; then
    captain=$ids
    say "captain member id from 'slack auth list': $captain"
  fi
fi
if [ -z "$captain" ]; then
  if keychain_has slack-bridge.captain-user-id; then
    say "keeping the captain member id already in the Keychain"
  elif [ -r /dev/tty ]; then
    printf 'Your Slack member id (Profile > ... > Copy member ID): ' >/dev/tty
    read -r captain </dev/tty
  else
    say "cannot tell your Slack member id; set SLACK_BRIDGE_CAPTAIN_ID and rerun"
    exit 1
  fi
fi
[ -z "$captain" ] || keychain_store slack-bridge.captain-user-id "$captain"

# The tokens live in the Keychain now; install-service.sh and the LaunchAgent read
# them from there, so they do not travel any further in this environment.
unset bot app SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_CLI_XOXB SLACK_CLI_XAPP
mark tokens-stored

if [ "${SLACK_BRIDGE_NO_INSTALL:-0}" = 1 ]; then
  say "skipping scripts/install-service.sh (SLACK_BRIDGE_NO_INSTALL=1)"
  exit 0
fi
exec "$repo/scripts/install-service.sh"
