# shellcheck shell=bash
# keychain.sh - Keychain helpers shared by slack-deploy-hook.sh and setup.sh.
# Source it; it defines keychain_has and keychain_store and needs `say`.
# Override (tests): SLACK_BRIDGE_SECURITY (security binary).

security=${SLACK_BRIDGE_SECURITY:-security}
account=${USER:-$(id -un)}

keychain_has() {
  "$security" find-generic-password -a "$account" -s "$1" >/dev/null 2>&1
}

# `security -i` reads the command from stdin, so the secret never shows up in
# the process list. Tokens and member ids are [A-Za-z0-9-] only; refuse
# anything else rather than quote it.
keychain_store() {
  local service=$1 value=$2
  case "$value" in
    *[!A-Za-z0-9-]*|"") say "refusing to store a malformed value for $service"; return 1 ;;
  esac
  printf 'add-generic-password -U -a "%s" -s "%s" -w "%s"\n' "$account" "$service" "$value" |
    "$security" -i >/dev/null 2>&1 || true
  if ! keychain_has "$service"; then
    say "could not write Keychain item $service"
    return 1
  fi
  say "stored Keychain item $service"
}
