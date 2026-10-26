#!/usr/bin/env bash
# uninstall.sh - unload and remove the slack-bridge LaunchAgent.
#   scripts/uninstall.sh          keep state, venv and logs
#   scripts/uninstall.sh --purge  also delete state and logs (and the repo .venv)
# Keychain items are left alone; SETUP.md shows how to delete them.
set -euo pipefail

label=com.firstmate.slack-bridge
plist="$HOME/Library/LaunchAgents/$label.plist"

launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
rm -f "$plist"
echo "LaunchAgent $label removed"

if [ "${1:-}" = "--purge" ]; then
  repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
  rm -rf "$HOME/Library/Application Support/slack-bridge" "$HOME/Library/Logs/slack-bridge" "$repo/.venv"
  echo "state, logs and venv removed"
fi
