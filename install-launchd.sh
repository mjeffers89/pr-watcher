#!/usr/bin/env bash
#
# Install (or reinstall) the PR Watcher as a macOS launchd agent so the UI
# starts at login and restarts if it dies. Idempotent — safe to re-run.
#
# Usage:
#   ./install-launchd.sh              # label "pr-watcher"
#   PRW_LABEL=my-watcher ./install-launchd.sh
#
# Uninstall:
#   launchctl bootout gui/$(id -u)/pr-watcher
#   rm ~/Library/LaunchAgents/pr-watcher.plist
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="${PRW_LABEL:-pr-watcher}"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
RUN_SH="$REPO_DIR/run.sh"
LOG="$HOME/.pr-watcher/launchd.log"

# Homebrew bin first so gh / claude / python resolve the same as in a login
# shell. Adjust if your tools live elsewhere.
LAUNCHD_PATH="$HOME/.local/share/mise/shims:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.pr-watcher"

sed \
  -e "s#__LABEL__#${LABEL}#g" \
  -e "s#__RUN_SH__#${RUN_SH}#g" \
  -e "s#__PATH__#${LAUNCHD_PATH}#g" \
  -e "s#__LOG__#${LOG}#g" \
  "$REPO_DIR/pr-watcher.plist.template" > "$PLIST"

# Reload cleanly if already loaded.
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/${LABEL}"
launchctl kickstart -k "gui/$(id -u)/${LABEL}"

echo "Installed launchd agent '${LABEL}'."
echo "  plist: $PLIST"
echo "  log:   $LOG"
echo "  UI:    http://127.0.0.1:${PRW_PORT:-4747}"
echo
echo "Status:   launchctl print gui/$(id -u)/${LABEL} | grep state"
echo "Stop:     launchctl bootout gui/$(id -u)/${LABEL}"
