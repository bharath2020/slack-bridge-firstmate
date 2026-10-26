#!/usr/bin/env bash
# install-service.sh - set up the venv and load the slack-bridge LaunchAgent.
# Run this yourself (the captain); it is never run by an agent. New installs
# start from scripts/setup.sh, which runs this script for you.
#   scripts/install-service.sh            install deps, write + load the LaunchAgent
#   scripts/install-service.sh --no-load  install deps and write the plist only
#   scripts/install-service.sh --deps-only  only create .venv and install deps (the
#                                   Slack CLI hooks need it before `slack deploy`)
#   scripts/install-service.sh --skip-doctor  install and load without the doctor check
#                                   (setup.sh runs doctor itself first)
# Overrides (tests): SLACK_BRIDGE_LAUNCHCTL (launchctl binary). Sourcing the
# script defines reload_agent and stops before doing anything.
set -euo pipefail

label=com.firstmate.slack-bridge
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
support="$HOME/Library/Application Support/slack-bridge"
venv="$repo/.venv"
logs="$HOME/Library/Logs/slack-bridge"
plist="$HOME/Library/LaunchAgents/$label.plist"
launchctl=${SLACK_BRIDGE_LAUNCHCTL:-launchctl}

# reload_agent PLIST: replace the loaded agent with PLIST and start it.
# bootout returns before the old instance is gone, and a bootstrap that
# lands meanwhile fails with "5: Input/output error", so wait for the old
# one to go and retry the bootstrap a few times before giving up.
reload_agent() {
  local domain i
  domain="gui/$(id -u)"
  "$launchctl" bootout "$domain/$label" 2>/dev/null || true
  for i in $(seq 250); do
    "$launchctl" print "$domain/$label" >/dev/null 2>&1 || break
    sleep 0.1
  done
  for i in 1 2 3; do
    "$launchctl" bootstrap "$domain" "$1" && break
    if [ "$i" -eq 3 ]; then
      echo "could not load $label: launchctl bootstrap failed 3 times." >&2
      echo "The bridge is not running. Load it by hand with:" >&2
      echo "  launchctl bootstrap $domain '$1'" >&2
      return 1
    fi
    sleep 1
  done
  "$launchctl" kickstart -k "$domain/$label"
}
[ "${BASH_SOURCE[0]}" = "$0" ] || return 0

load=1 deps_only=0 doctor=1
case "${1:-}" in
  --no-load) load=0 ;;
  --deps-only) deps_only=1 ;;
  --skip-doctor) doctor=0 ;;
  "") ;;
  *) echo "usage: scripts/install-service.sh [--no-load|--deps-only|--skip-doctor]" >&2; exit 2 ;;
esac

python=${PYTHON:-python3}

echo "==> venv at $venv"
[ -x "$venv/bin/python3" ] || "$python" -m venv "$venv"
"$venv/bin/python3" -m pip install --quiet --upgrade pip
"$venv/bin/python3" -m pip install --quiet -r "$repo/requirements.txt"
[ "$deps_only" -eq 0 ] || { echo "deps installed in $venv"; exit 0; }

mkdir -p "$support" "$logs" "$HOME/Library/LaunchAgents"
chmod 700 "$support"

if [ "$doctor" -eq 1 ]; then
  echo "==> checking setup"
  "$repo/bin/slack-bridge" doctor || {
    echo "doctor reported problems; fix them (see SETUP.md) and rerun." >&2
    exit 1
  }
fi

# launchd starts with a bare PATH; give it the places herdr, claude and
# python live so the keeper and fm-inbox.sh find them.
path="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

echo "==> writing $plist"
esc() { printf '%s' "$1" | sed -e 's/\&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/[\&|\\]/\\&/g'; }
sed -e "s|@REPO@|$(esc "$repo")|g" -e "s|@HOME@|$(esc "$HOME")|g" \
    -e "s|@PATH@|$(esc "$path")|g" \
    "$repo/launchd/$label.plist.in" > "$plist"
plutil -lint "$plist" >/dev/null

if [ "$load" -eq 1 ]; then
  echo "==> loading $label"
  reload_agent "$plist"
  echo "loaded. Logs: $logs/bridge.log"
else
  echo "not loaded (--no-load). Load later with:"
  echo "  launchctl bootstrap gui/$(id -u) '$plist'"
fi
