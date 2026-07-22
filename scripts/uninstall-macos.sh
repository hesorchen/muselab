#!/usr/bin/env bash
# muselab — macOS uninstaller. Removes LaunchAgents and the status bar client.
# Leaves the repo .env, sessions/, archive, and log directory untouched.
set -euo pipefail

PLIST="$HOME/Library/LaunchAgents/com.muselab.plist"
STATUSBAR_PLIST="$HOME/Library/LaunchAgents/com.muselab.statusbar.plist"
STATUSBAR_DIR="$HOME/Library/Application Support/muselab"
DOMAIN="gui/$(id -u)"

ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
err()  { printf "  \033[31m✗\033[0m %s\n" "$*" >&2; }

if [[ "$(uname -s)" != "Darwin" ]]; then
  err "This uninstaller must run on macOS."
  exit 1
fi
if [[ $EUID -eq 0 ]]; then
  err "Don't run this with sudo; the LaunchAgents belong to your login user."
  exit 1
fi

echo "muselab — uninstall (macOS)"

for entry in \
  "$STATUSBAR_PLIST|com.muselab.statusbar|status bar LaunchAgent" \
  "$PLIST|com.muselab|backend LaunchAgent"; do
  IFS='|' read -r path label description <<< "$entry"
  launchctl bootout "$DOMAIN/$label" 2>/dev/null \
    || launchctl unload "$path" 2>/dev/null || true
  if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
    err "$description is still running; not deleting $path"
    exit 1
  fi
  if [[ -f "$path" ]]; then
    rm -f "$path"
    ok "$description removed: $path"
  else
    warn "no plist at $path — nothing to remove"
  fi
done

if [[ -d "$STATUSBAR_DIR" ]]; then
  rm -f "$STATUSBAR_DIR/MuseLabStatusBar" "$STATUSBAR_DIR/statusbar.env"
  rmdir "$STATUSBAR_DIR" 2>/dev/null || true
  ok "status bar helper and remote credentials removed"
fi

echo
echo "Note: repo .env, sessions/, your MUSELAB_ROOT, and ~/Library/Logs/muselab"
echo "are NOT touched. Delete the repo to fully remove muselab."
