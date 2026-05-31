#!/usr/bin/env bash
# muselab — macOS uninstaller. Removes the LaunchAgent.
# Leaves .env, sessions/, your archive, and the log dir untouched.
set -euo pipefail

PLIST="$HOME/Library/LaunchAgents/com.muselab.plist"

ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }

echo "muselab — uninstall (macOS)"

if [[ -f "$PLIST" ]]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  ok "LaunchAgent removed: $PLIST"
else
  warn "no plist at $PLIST — nothing to remove"
fi

# Some launchctl versions also need an explicit bootout
if launchctl list 2>/dev/null | grep -q com.muselab; then
  launchctl bootout "gui/$(id -u)/com.muselab" 2>/dev/null || true
fi

echo
echo "Note: .env, sessions/, your MUSELAB_ROOT, and ~/Library/Logs/muselab"
echo "are NOT touched. Delete the repo to fully remove muselab."
