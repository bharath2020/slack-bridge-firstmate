#!/usr/bin/env bash
# Install from a clone: ./scripts/install.sh
# Or download this script and pipe it to bash; prompts use /dev/tty.
# No sudo. One bridge/workspace per macOS login account.
set -euo pipefail

main() {
  local source_dir='' repo='' target url python
  case "${1:-}" in
    -h|--help)
      echo "usage: ./scripts/install.sh"
      echo "Remote install: curl -fsSL <raw install.sh URL> | bash"
      echo "Overrides: SLACK_BRIDGE_INSTALL_DIR, SLACK_BRIDGE_REPO_URL, PYTHON"
      return 0 ;;
    "") ;;
    *) echo "Unknown option. Use --help." >&2; return 2 ;;
  esac
  if [ -f "${BASH_SOURCE[0]:-}" ]; then
    source_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
    if [ -f "$source_dir/requirements.txt" ] && [ -x "$source_dir/scripts/setup.sh" ]; then
      repo=$source_dir
    fi
  fi
  if [ -z "$repo" ]; then
    [ "$(uname -s)" = Darwin ] || { echo "Slack bridge requires macOS." >&2; return 1; }
    python=${PYTHON:-python3}
    command -v "$python" >/dev/null 2>&1 || { echo "Install Python 3.9+ first." >&2; return 1; }
    "$python" -c 'import sys; sys.exit(sys.version_info < (3, 9))' || {
      echo "Python 3.9 or newer is required. Set PYTHON to a newer executable." >&2; return 1;
    }
    command -v git >/dev/null 2>&1 || { echo "Install Git: xcode-select --install" >&2; return 1; }
    command -v "${SLACK_BRIDGE_HERDR:-herdr}" >/dev/null 2>&1 || {
      echo "Install Herdr and put it on PATH before installing the bridge." >&2; return 1;
    }
    target=${SLACK_BRIDGE_INSTALL_DIR:-$HOME/.local/share/slack-bridge}
    url=${SLACK_BRIDGE_REPO_URL:-https://github.com/bharath2020/slack-bridge-firstmate.git}
    case "$target" in /*) ;; *) echo "SLACK_BRIDGE_INSTALL_DIR must be an absolute path." >&2; return 1 ;; esac
    if [ -e "$target" ]; then
      repo=$(cd "$target" && pwd -P)
      [ "$(git -C "$repo" rev-parse --show-toplevel 2>/dev/null)" = "$repo" ] || {
        echo "Install directory already exists and is not a repository; choose another directory." >&2; return 1;
      }
      [ "$(git -C "$repo" remote get-url origin)" = "$url" ] || {
        echo "Install directory has a different origin; choose another directory." >&2; return 1;
      }
      [ -z "$(git -C "$repo" status --porcelain)" ] || {
        echo "Local changes found. Commit or move them before updating; nothing was overwritten." >&2; return 1;
      }
      echo "==> Updating bridge (fast-forward only)"
      git -C "$repo" fetch --quiet origin main
      git -C "$repo" merge --ff-only --quiet FETCH_HEAD
    else
      mkdir -p "$(dirname "$target")"
      echo "==> Cloning bridge"
      git clone --quiet --branch main -- "$url" "$target"
      repo=$(cd "$target" && pwd -P)
    fi
  fi
  # With curl | bash, stdin contains the script, not wizard answers.
  if [ ! -t 0 ]; then
    if ! { exec 3</dev/tty; } 2>/dev/null; then
      echo "Setup needs an interactive terminal. Run ./scripts/install.sh from a terminal." >&2
      return 1
    fi
    exec "$repo/scripts/setup.sh" <&3
  fi
  exec "$repo/scripts/setup.sh"
}
main "$@"
