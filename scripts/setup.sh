#!/usr/bin/env bash
# setup.sh - one command from a fresh clone to a working bridge.
#   scripts/setup.sh
# Walks you (the captain) through it, one question at a time: Python and the
# Slack CLI (offers to install the official CLI), the project venv, your
# Firstmate folder, `slack login`, `slack deploy` (creates the app from
# manifest.json, or updates the one this clone already made), the LaunchAgent,
# and a live check with a test DM. Safe to rerun: finished steps are detected
# and skipped or refreshed. No token is ever printed: the deploy hook stores
# them in the Keychain over stdin, and the one you may paste is read hidden.
#
# Overrides (tests): SLACK_BRIDGE_SLACK, SLACK_BRIDGE_SECURITY,
# SLACK_BRIDGE_LAUNCHCTL, SLACK_BRIDGE_INSTALL (install-service.sh), SLACK_BRIDGE_CLI
# (bin/slack-bridge), SLACK_BRIDGE_SLACK_INSTALLER (installer URL),
# SLACK_BRIDGE_CONNECT_CHECK (app-level token check), SLACK_BRIDGE_SETTLE_SECONDS
# (how long the bridge must stay up in the live check), PYTHON.
set -euo pipefail

label=com.firstmate.slack-bridge
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
support="$HOME/Library/Application Support/slack-bridge"
fm_home_file="$support/fm-home"
workspace_file="$support/workspace"
org_workspace_file="$support/org-workspace"
app_file="$support/app-id"
marker="$support/last-deploy"
logs="$HOME/Library/Logs/slack-bridge"
launchctl=${SLACK_BRIDGE_LAUNCHCTL:-launchctl}
installer=${SLACK_BRIDGE_INSTALL:-$repo/scripts/install-service.sh}
bridge=${SLACK_BRIDGE_CLI:-$repo/bin/slack-bridge}
slack_installer=${SLACK_BRIDGE_SLACK_INSTALLER:-https://downloads.slack-edge.com/slack-cli/install.sh}
connect_check=${SLACK_BRIDGE_CONNECT_CHECK:-}
workspace_check=${SLACK_BRIDGE_WORKSPACE_CHECK:-}
settle=${SLACK_BRIDGE_SETTLE_SECONDS:-5}
python=${PYTHON:-python3}
slack_cli=
fm_home=
captain=
team=
org_workspace=
bound=0
total=7

if [ -t 1 ]; then
  bold=$'\033[1m' green=$'\033[32m' yellow=$'\033[33m' red=$'\033[31m' reset=$'\033[0m'
else
  bold='' green='' yellow='' red='' reset=''
fi

say() { printf '    %s\n' "$*"; }
ok() { printf '    %s✓%s %s\n' "$green" "$reset" "$*"; }
warn() { printf '    %s!%s %s\n' "$yellow" "$reset" "$*"; }
step() { printf '\n%s[%s/%s] %s%s\n' "$bold" "$1" "$total" "$2" "$reset"; }

# fail PROBLEM [WHAT-TO-DO...]: say what went wrong and what to do next, then stop.
fail() {
  printf '\n%s✗ %s%s\n' "$red" "$1" "$reset" >&2
  shift
  local next
  for next in "$@"; do printf '  %s\n' "$next" >&2; done
  printf '  Then run scripts/setup.sh again; it picks up where it stopped.\n' >&2
  exit 1
}

# confirm QUESTION y|n: a yes/no question; Enter (or no input) takes the default.
confirm() {
  local hint=y/N answer
  [ "$2" = y ] && hint=Y/n
  printf '    %s [%s] ' "$1" "$hint"
  read -r answer || { answer=; echo; }
  [ -n "$answer" ] || answer=$2
  case "$answer" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

# ask VAR QUESTION [DEFAULT]: one free-text question into VAR.
ask() {
  local answer
  if [ -n "${3:-}" ]; then printf '    %s [%s]: ' "$2" "$3"; else printf '    %s: ' "$2"; fi
  read -r answer || { answer=; echo; }
  printf -v "$1" '%s' "${answer:-${3:-}}"
}

# shellcheck source=scripts/keychain.sh
. "$repo/scripts/keychain.sh"

# --- 1. prerequisites -------------------------------------------------------

check_system() {
  [ "$(uname -s)" = Darwin ] ||
    fail "The bridge runs on macOS only (it uses the login Keychain and launchd)."
  command -v "$python" >/dev/null 2>&1 ||
    fail "Python 3 is not installed." "Install it with: xcode-select --install" \
         "(or, with Homebrew: brew install python)"
  "$python" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null ||
    fail "Python 3.9 or newer is needed; found $("$python" --version 2>&1)." \
         "Install a newer one, for example: brew install python"
  command -v "${SLACK_BRIDGE_HERDR:-herdr}" >/dev/null 2>&1 ||
    fail "Herdr is not installed or is not on PATH." "Install Herdr first, then open a new terminal."
  ok "$("$python" --version 2>&1)"
}

find_slack() {
  local c
  for c in "${SLACK_BRIDGE_SLACK:-}" "$(command -v slack 2>/dev/null || true)" \
           "$HOME/.slack/bin/slack" "$HOME/.local/bin/slack"; do
    if [ -n "$c" ] && [ -x "$c" ]; then slack_cli=$c; return 0; fi
  done
  return 1
}

ensure_slack_cli() {
  local version major minor
  if ! find_slack; then
    warn "The Slack CLI is not installed. Setup uses it to log you in and create the app."
    confirm "Install the official Slack CLI now (from downloads.slack-edge.com)?" y ||
      fail "The Slack CLI is needed." \
           "Install it from https://api.slack.com/automation/cli/install-mac, or rerun and answer yes."
    say "Installing the Slack CLI..."
    curl -fsSL "$slack_installer" | bash ||
      fail "The Slack CLI install did not finish." \
           "Check your internet connection and the installer's message above."
    find_slack ||
      fail "The Slack CLI installed, but setup cannot find the slack command." \
           "Open a new terminal window so your PATH picks it up."
  fi
  version=$("$slack_cli" version --skip-update 2>/dev/null |
            sed -n 's/.*v\([0-9][0-9]*\.[0-9][0-9.]*\).*/\1/p' | head -n 1 || true)
  [ -n "$version" ] ||
    fail "'$slack_cli' does not look like the Slack CLI ('slack version' printed no version)." \
         "Install the official one: https://api.slack.com/automation/cli/install-mac"
  major=${version%%.*} minor=${version#*.}
  minor=${minor%%.*}
  if [ "$major" -lt 4 ] || { [ "$major" -eq 4 ] && [ "$minor" -lt 11 ]; }; then
    warn "Slack CLI v$version is older than the v4.11 this setup needs."
    if ! confirm "Update it now (slack upgrade)?" y || ! "$slack_cli" upgrade; then
      fail "The Slack CLI is too old." "Update it with: slack upgrade"
    fi
  fi
  ok "Slack CLI v$version ($slack_cli)"
  # The CLI's hooks run `python3`; the venv's comes first so they find
  # slack-cli-hooks, and the deploy hook finds this same slack binary.
  PATH="$repo/.venv/bin:$(dirname "$slack_cli"):$PATH"
  export PATH
}

# --- 2. Python packages -----------------------------------------------------

install_deps() {
  if [ -x "$repo/.venv/bin/python3" ]; then
    say "Refreshing the packages in $repo/.venv..."
  else
    say "Creating $repo/.venv and installing the packages (a minute or so)..."
  fi
  PYTHON=$python "$installer" --deps-only >/dev/null ||
    fail "Installing the Python packages failed." \
         "Check your internet connection and the pip error above."
  ok "Packages installed"
}

# --- 3. Firstmate folder ----------------------------------------------------

fm_home_ok() { [ -n "$1" ] && [ -x "$1/bin/fm-inbox.sh" ]; }

# find_fm_home: print the first Firstmate home that has bin/fm-inbox.sh: the
# env override, the saved choice, the folder this clone sits in
# (<home>/projects/slack-bridge), then the usual places.
find_fm_home() {
  local c saved=
  [ -r "$fm_home_file" ] && saved=$(head -n 1 "$fm_home_file")
  for c in "${SLACK_BRIDGE_FM_HOME:-}" "$saved" "$(cd "$repo/../.." && pwd -P)" \
           "$HOME/Documents/firstmate" "$HOME/firstmate"; do
    if fm_home_ok "$c"; then printf '%s\n' "$c"; return 0; fi
  done
  return 1
}

choose_fm_home() {
  local found path tries=0
  if found=$(find_fm_home); then
    say "Found Firstmate at $found"
    confirm "Use this Firstmate folder?" y && fm_home=$found
  else
    say "Setup could not find your Firstmate folder (the one with bin/fm-inbox.sh)."
  fi
  while [ -z "$fm_home" ]; do
    ask path "Path to your Firstmate folder"
    path=${path/#\~/$HOME}
    if fm_home_ok "$path"; then
      fm_home=$(cd "$path" && pwd -P)
    else
      tries=$((tries + 1))
      [ "$tries" -lt 3 ] ||
        fail "No Firstmate folder found." \
             "It is the folder with bin/fm-inbox.sh in it; check the path and that Firstmate is installed."
      warn "${path:-That} has no bin/fm-inbox.sh; try again."
    fi
  done
  mkdir -p "$support"
  chmod 700 "$support"
  ok "Slack messages will go to $fm_home/bin/fm-inbox.sh"
}

# --- 4. Slack login ---------------------------------------------------------

# auth_accounts: one "member-id<TAB>team-id<TAB>workspace" line per account
# `slack login` authorized; the workspace is the heading line above the
# account's fields, and the team id ("-" if unknown) is in it or its own field.
auth_accounts() {
  "$slack_cli" auth list --no-color --skip-update 2>/dev/null | awk '
    function team_id(s) { if (match(s, /Team ID:[ \t]*[TE][A-Z0-9]+/)) { s = substr(s, RSTART, RLENGTH); sub(/Team ID:[ \t]*/, "", s); return s } return "" }
    { sub(/\r$/, "") }
    /User ID:/ {
      id = $0; sub(/.*User ID:[ \t]*/, "", id); sub(/[^A-Z0-9].*/, "", id)
      if (id ~ /^[UW][A-Z0-9]+$/) print id "\t" (tid == "" ? "-" : tid) "\t" (team == "" ? "your workspace" : team)
      next
    }
    /^[ \t]*Team ID[ \t]*:/ { tid = team_id($0); next }
    /^[ \t]*$/ || /^[ \t]*(Last Updated|Authorization Level|Enterprise ID|Team Domain)[ \t]*:/ { next }
    { team = $0; gsub(/^[ \t]+|[ \t]+$/, "", team); tid = team_id(team) }' || true
}

# Only recognized fields are shown, never arbitrary CLI output or credentials.
auth_diagnostic() {
  "$slack_cli" auth list --no-color --skip-update 2>/dev/null | "$python" -c '
import re, sys
text = sys.stdin.read()
def clean(value):
    return re.sub(r"(?:xox[a-z]-|xapp-)[A-Za-z0-9-]+", "<redacted>", value)[:160]
for line in text.splitlines():
    if "(Team ID:" in line:
        print("    Account: " + clean(line.strip()))
    elif re.match(r"\s*(Team ID|User ID|Authorization Level)\s*:", line):
        print("    " + clean(line.strip()))
' || true
}

require_accounts() {
  [ -z "$accounts" ] || return 0
  auth_diagnostic
  fail "No usable account could be read from slack auth list." \
       "If account details appear above, login exists but its format was not recognized." \
       "Update with: slack upgrade; then rerun setup. If no account is listed, run: slack login" \
       "If it persists, report the safe account details at https://github.com/bharath2020/slack-bridge/issues."
}

slack_login() {
  say "Logging in links the Slack CLI to your workspace. In a moment:"
  say "  1. The terminal prints a command that starts with /slackauthticket."
  say "  2. Copy it, paste it into any message box in Slack (your own DM is fine) and send it."
  say "  3. Approve the request Slack shows you."
  say "  4. Slack then shows a short challenge code: type it back here."
  ( cd "$repo" && "$slack_cli" login ) ||
    fail "Slack login did not finish." \
         "Run setup again and paste the /slackauthticket line into Slack within a few minutes."
}

# Identify legacy credentials before overwriting any Keychain item. Only a
# team id or a generic failure is printed, never the token or response body.
stored_workspace() {
  if [ -n "$workspace_check" ]; then "$workspace_check"; return; fi
  PYTHONPATH=$repo "$python" - <<'EOF'
import json, re, sys, urllib.request
from slack_bridge.config import KEYCHAIN_BOT_TOKEN, keychain_read
try:
    token = keychain_read(KEYCHAIN_BOT_TOKEN)
    req = urllib.request.Request("https://slack.com/api/auth.test", data=b"",
                                 headers={"Authorization": f"Bearer {token}"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as response:
        body = json.load(response)
    team = body.get("team_id") or body.get("enterprise_id") or ""
    if not body.get("ok") or not re.fullmatch(r"[TE][A-Z0-9]+", team):
        raise ValueError("unverified workspace")
except Exception:
    sys.exit("Could not verify the workspace of existing Keychain credentials.")
print(team)
EOF
}

pick_account() {
  local accounts n choice line listing stored=
  accounts=$(auth_accounts)
  if [ -n "$accounts" ]; then
    n=$(printf '%s\n' "$accounts" | wc -l | tr -d ' ')
    if [ "$n" = 1 ]; then
      say "Logged in to $(printf '%s' "$accounts" | cut -f 3) as ${accounts%%$'\t'*}"
      confirm "Set the bridge up for this workspace?" y || { slack_login; accounts=$(auth_accounts); }
    fi
  else
    # An unrecognized record is different from an absent login. Do not ask
    # someone who is already logged in to repeat the login challenge.
    listing=$("$slack_cli" auth list --no-color --skip-update 2>/dev/null || true)
    case "$listing" in *'User ID:'*) require_accounts ;; esac
    slack_login
    accounts=$(auth_accounts)
  fi
  require_accounts
  n=$(printf '%s\n' "$accounts" | wc -l | tr -d ' ')
  choice=1
  if [ "$n" != 1 ]; then
    say "You are logged in to more than one workspace:"
    printf '%s\n' "$accounts" | awk -F '\t' '{ printf "      %d. %s (%s)\n", NR, $3, $1 }'
    while :; do
      ask choice "Which one is the bridge for? (1-$n)" 1
      case "$choice" in *[!0-9]*|"") ;; *) [ "$choice" -ge 1 ] && [ "$choice" -le "$n" ] && break ;; esac
      warn "Type a number from 1 to $n."
    done
  fi
  line=$(printf '%s\n' "$accounts" | sed -n "${choice}p")
  captain=${line%%$'\t'*}
  team=$(printf '%s' "$line" | cut -f 2)
  [ "$team" != - ] || team=
  [ -n "$team" ] ||
    { auth_diagnostic; fail "Could not identify the selected workspace." \
      "The selected account has no supported T workspace or E organization ID." \
      "Update the Slack CLI and check: slack auth list"; }
  if [ -s "$workspace_file" ]; then
    stored=$(cat "$workspace_file")
  elif keychain_has slack-bridge.bot-token; then
    stored=$(stored_workspace) ||
      fail "Could not verify the workspace of existing Keychain credentials." \
           "Check network access and that the old app is still installed." \
           "For revoked credentials, follow the replacement procedure in SETUP.md."
  fi
  if [ -n "$stored" ] && [ "$stored" != "$team" ]; then
    fail "This macOS account already has a bridge for another workspace." \
         "Use a different Mac or macOS account for the work workspace." \
         "To replace the old bridge, follow the workspace-switch steps in SETUP.md first."
  fi
  [ -z "$stored" ] || bound=1
  if [[ "$team" = E* ]]; then
    say "This login covers an Enterprise Grid organization, not a single workspace."
    say "Choose one workspace in that organization for this bridge."
    say "In Slack's browser URL app.slack.com/client/T.../..., copy the T... workspace ID."
    local saved_workspace='' tries=0
    [ ! -s "$org_workspace_file" ] || saved_workspace=$(cat "$org_workspace_file")
    while :; do
      ask org_workspace "Workspace ID inside this organization (starts with T)" "$saved_workspace"
      [[ "$org_workspace" =~ ^T[A-Z0-9]+$ ]] && break
      tries=$((tries + 1))
      [ "$tries" -lt 3 ] || fail "No workspace ID was selected." "Use the T... ID from your intended workspace's Slack browser URL."
      warn "That is not a workspace ID; use T followed by uppercase letters and digits."
    done
    if [ -n "$saved_workspace" ] && [ "$org_workspace" != "$saved_workspace" ]; then
      fail "This bridge is already bound to another workspace in this organization." \
           "Follow the workspace-switch steps in SETUP.md before replacing it."
    fi
  fi
  # Pass the selected member explicitly; auth list may contain several accounts.
  keychain_store slack-bridge.captain-user-id "$captain" >/dev/null ||
    fail "Could not save your member id in the Keychain." "Unlock your login keychain (Keychain Access) and try again."
  ok "The bridge will talk to you as $captain in $(printf '%s' "$line" | cut -f 3)"
}

# --- 5. the Slack app -------------------------------------------------------

app_token_fallback() {
  local token msg tries=0
  warn "Slack did not hand over the app-level token the bridge connects with."
  say "Make one in your browser; it takes a minute:"
  say "  1. Open your app's settings page (setup can open it for you)."
  say "  2. Under Basic Information, scroll to App-Level Tokens and click Generate Token and Scopes."
  say "  3. Name it bridge, click Add Scope, pick connections:write, then click Generate."
  say "  4. Click Copy next to the new token (it starts with xapp-)."
  if confirm "Open the app settings page now?" y; then
    ( cd "$repo" && "$slack_cli" app settings ) ||
      warn "Could not open it; run 'slack app settings' in another terminal, from this folder."
  fi
  while :; do
    printf '    Paste the xapp- token here (it stays hidden), then press Enter: '
    IFS= read -rs token || token=
    echo
    token=${token//[[:space:]]/}
    case "$token" in
      xapp-*)
        if msg=$(keychain_store slack-bridge.app-token "$token"); then break; fi
        warn "Not saved: ${msg#"    "}. If your login keychain is locked, unlock it in Keychain Access first." ;;
      xoxb-*|xoxp-*) warn "That is a bot or user token; the bridge needs the app-level one (xapp-)." ;;
      "") warn "Nothing was pasted." ;;
      *) warn "That does not look like an app-level token; it should start with xapp-." ;;
    esac
    tries=$((tries + 1))
    [ "$tries" -lt 3 ] ||
      fail "No app-level token was saved." "Generate one as described above (scope connections:write)."
  done
  unset token
  ok "App-level token saved in the Keychain"
}

# app_id: this clone's Slack app id for the selected workspace, from the
# Slack CLI's .slack/apps.json, or nothing.
app_id() {
  "$python" - "$repo/.slack/apps.json" "$team" 2>/dev/null <<'EOF' || true
import json, sys
apps = json.load(open(sys.argv[1])).get("apps", {})
ids = {a.get("app_id") for k, a in apps.items() if sys.argv[2] in (k, a.get("team_id"))}
if len(ids) == 1 and None not in ids:
    print(ids.pop())
EOF
}

deploy_app() {
  local rc=0 outcome keep=0 app
  local -a args=()
  [ -z "$team" ] || args=(--team "$team")
  if [ -n "$org_workspace" ]; then
    args+=(--org-workspace-grant "$org_workspace")
    say "Slack will grant the app access to workspace $org_workspace in organization $team."
    say "If Slack asks for a workspace, choose that workspace, not all workspaces."
    say "If org-admin approval is required, request it in the CLI and wait for Slackbot's approval message."
    say "Then rerun setup and choose the existing app to finish installation."
  fi
  # .slack/apps.json is the Slack CLI's record of the app this clone made.
  if [ -s "$repo/.slack/apps.json" ]; then
    app=$(app_id)
    if [ "$bound" = 1 ] && [ -n "$app" ] && [ "$(cat "$app_file" 2>/dev/null)" = "$app" ]; then keep=1; fi
    if [ -s "$workspace_file" ] &&
       keychain_has slack-bridge.bot-token && keychain_has slack-bridge.app-token; then
      say "This clone already has its Slack app, and its tokens are in the Keychain."
      if ! confirm "Update the app now (applies manifest.json changes, refreshes its tokens)?" y; then
        ok "Keeping the app as it is"
        return 0
      fi
    fi
    say "When the Slack CLI asks which app, pick your existing Firstmate app, not \"Create a new app\"."
  else
    say "The Slack CLI now creates the Firstmate app and installs it in your workspace."
    if [ -n "$team" ]; then
      say "When it asks, choose \"Create a new app\". Approve the permissions it lists."
    else
      say "When it asks, choose \"Create a new app\", then your workspace. Approve the permissions it lists."
    fi
  fi
  rm -f "$marker"
  ( cd "$repo" && SLACK_BRIDGE_CAPTAIN_ID="$captain" SLACK_BRIDGE_KEEP_APP_TOKEN=$keep SLACK_BRIDGE_SETUP=1 SLACK_BRIDGE_NO_INSTALL=1 "$slack_cli" deploy ${args[@]+"${args[@]}"} ) || rc=$?
  outcome=$(cat "$marker" 2>/dev/null || true)
  case "$outcome" in
    tokens-stored)
      [ "$rc" -eq 0 ] || fail "slack deploy failed after storing tokens (exit $rc)." "Read Slack's error and rerun setup."
      ;;
    no-app-token) app_token_fallback ;;
    *)
      fail "slack deploy did not confirm installation through the bridge hook (exit $rc)." \
           "Scroll up for Slack's actual result; an exit code of 0 can mean admin approval is pending." \
           "If approval was requested, wait for Slackbot's approval message, then rerun setup and choose the existing app." \
           "If Slack reported an error or cancelled installation, resolve that message before rerunning setup."
      ;;
  esac
  printf '%s\n' "$team" >"$workspace_file"
  if [ -n "$org_workspace" ]; then printf '%s\n' "$org_workspace" >"$org_workspace_file"; fi
  app=$(app_id)
  if [ -n "$app" ]; then printf '%s\n' "$app" >"$app_file"; else rm -f "$app_file"; fi
  ok "Firstmate app installed; its tokens are in the Keychain"
}

# --- 6. background service --------------------------------------------------

install_agent() {
  local report rc=0 fatal
  # Persist only after workspace selection and deployment succeed.
  printf '%s\n' "$fm_home" >"$fm_home_file"
  report=$("$bridge" doctor 2>&1) || rc=$?
  printf '%s\n' "$report" | sed 's/^/      /'
  if [ "$rc" -ne 0 ]; then
    fatal=$(printf '%s\n' "$report" | grep '^FAIL' | grep -E 'Keychain|fm-inbox|slack_bolt' || true)
    [ -z "$fatal" ] ||
      fail "The setup check found a problem (the FAIL lines above)." \
           "Keychain: rerun setup and update the app. fm-inbox.sh: check the Firstmate folder." \
           "slack_bolt: rerun setup to reinstall the packages."
    warn "The bridge doctor returned exit $rc; review its report above before continuing."
    say "The bridge still takes your Slack messages and hands them to Firstmate's inbox."
    confirm "Install the background service anyway?" y ||
      fail "Stopped before installing the background service." "Resolve the doctor's reported problem first."
  fi
  "$installer" --skip-doctor >/dev/null ||
    fail "Could not install the background service." "See the launchctl message above."
  ok "Background service $label installed; it starts at login and restarts if it stops"
}

# --- 7. live check ----------------------------------------------------------

# agent_pid: the pid of the running LaunchAgent, or nothing.
agent_pid() {
  "$launchctl" print "gui/$(id -u)/$label" 2>/dev/null |
    awk '$1 == "pid" && $2 == "=" && $3 ~ /^[0-9]+$/ { print $3; exit }' || true
}

# check_app_token: open a Socket Mode connection URL with the app-level token
# in the Keychain, as the bridge does to receive messages. The token goes in
# a header, never argv or output; on failure prints only the reason. Exits 3
# when Slack is rate-limiting the check, which says nothing about the token.
check_app_token() {
  if [ -n "$connect_check" ]; then "$connect_check"; return; fi
  PYTHONPATH=$repo "$repo/.venv/bin/python3" - <<'EOF'
import json, sys, time, urllib.error, urllib.request
from slack_bridge.config import KEYCHAIN_APP_TOKEN, ConfigError, keychain_read
try:
    token = keychain_read(KEYCHAIN_APP_TOKEN)
except ConfigError:
    sys.exit("the app-level token is not in the Keychain")
req = urllib.request.Request("https://slack.com/api/apps.connections.open", data=b"",
                             headers={"Authorization": f"Bearer {token}"}, method="POST")
for attempt in (1, 2):
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.load(resp)
        break
    except urllib.error.HTTPError as exc:
        if exc.code != 429:
            sys.exit(f"Slack answered HTTP {exc.code}")
        if attempt == 2:
            print("Slack is rate-limiting the check", file=sys.stderr)
            sys.exit(3)
        try:
            wait = int(exc.headers.get("Retry-After", "5"))
        except ValueError:
            wait = 5
        time.sleep(min(max(wait, 1), 30))
    except (OSError, ValueError) as exc:
        sys.exit(f"could not reach Slack ({exc.__class__.__name__})")
if not body.get("ok"):
    sys.exit(f"Slack answered {body.get('error', 'an unknown error')}")
EOF
}

live_check() {
  local i pid reason rc=0
  for i in $(seq 30); do
    pid=$(agent_pid)
    [ -z "$pid" ] || break
    [ "$i" -lt 30 ] ||
      fail "The background service is installed but not running." \
           "Look at $logs/bridge.log and $logs/launchd.err.log for the reason."
    sleep 1
  done
  ok "The bridge is running"
  reason=$(check_app_token 2>&1) || rc=$?
  if [ "$rc" -eq 3 ]; then
    warn "Could not verify the app-level token: Slack is rate-limiting the check right now."
    warn "If messages from Slack do not reach the bridge, rerun setup in a minute."
  elif [ "$rc" -ne 0 ]; then
    fail "The bridge cannot connect to Slack: ${reason##*$'\n'}." \
         "Until this is fixed, messages you send from Slack will not reach the bridge." \
         "Usually the app-level token was revoked or lacks connections:write. Make a new one:" \
         "run 'slack app settings', open Basic Information > App-Level Tokens > Generate Token and Scopes," \
         "add the scope connections:write, copy the xapp- token, then store it (it asks for the token):" \
         "  security add-generic-password -U -a \"\$USER\" -s slack-bridge.app-token -w" \
         "If Slack could not be reached, check your internet connection instead."
  else
    ok "Slack accepts the app-level token, so messages from Slack can reach the bridge"
  fi
  "$bridge" say "Setup check: the bridge can reach you here." >/dev/null ||
    fail "The bridge could not post a test message to your DM." \
         "Rerun setup and update the app; if it still fails, look at $logs/bridge.log."
  ok "Sent you a test DM from Firstmate"
  sleep "$settle"
  [ "$(agent_pid)" = "$pid" ] ||
    fail "The bridge stopped or restarted right after starting, so messages from Slack will not reach it." \
         "Look at $logs/bridge.log and $logs/launchd.err.log for the reason."
  ok "The bridge is still running after ${settle}s"
  if ! confirm "Did it arrive in Slack (Apps > Firstmate)?" y; then
    warn "Check that Slack is open on the workspace you picked, and look under Apps for Firstmate."
    warn "Still nothing? Rerun setup and update the app, then look at $logs/bridge.log."
  fi
}

finish() {
  printf '\n%sAll set.%s Try it from Slack, in the DM with Firstmate:\n' "$bold" "$reset"
  say "status               Firstmate's inbox status comes back in a thread"
  say "ping from my phone   gets 👀 at once, then Firstmate's reply in the same thread"
  say "From this Mac:       bin/slack-bridge say \"hello\""
  say "Logs:                $logs/bridge.log (no message text, no tokens)"
  say "Rerun scripts/setup.sh any time: it updates the existing install."
}

main() {
  case "${1:-}" in
    "") ;;
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; return 0 ;;
    *) echo "usage: scripts/setup.sh" >&2; return 2 ;;
  esac
  printf '%sSlack bridge setup%s: about five minutes. Keep Slack open; you approve two things there.\n' \
    "$bold" "$reset"
  step 1 "Checking Python and the Slack CLI"; check_system; ensure_slack_cli
  step 2 "Installing the bridge's Python packages"; install_deps
  step 3 "Finding your Firstmate folder"; choose_fm_home
  step 4 "Logging in to Slack"; pick_account
  step 5 "Creating or updating the Slack app"; deploy_app
  step 6 "Starting the background service"; install_agent
  step 7 "Checking it end to end"; live_check
  finish
}

# Sourcing the script defines its functions and stops (tests).
[ "${BASH_SOURCE[0]}" = "$0" ] || return 0
main "$@"
