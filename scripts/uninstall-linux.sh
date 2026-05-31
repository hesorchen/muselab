#!/usr/bin/env bash
# muselab — Linux uninstaller. Stops & removes the systemd --user service.
# Leaves .env, sessions/, and your archive untouched.
set -euo pipefail

UNIT="$HOME/.config/systemd/user/muselab.service"

ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }

echo "muselab — uninstall (Linux)"

if systemctl --user list-unit-files muselab.service >/dev/null 2>&1; then
  systemctl --user disable --now muselab.service 2>/dev/null || true
  ok "service stopped & disabled"
fi

if [[ -f "$UNIT" ]]; then
  rm -f "$UNIT"
  ok "unit file removed: $UNIT"
else
  warn "no unit file at $UNIT — nothing to remove"
fi

systemctl --user daemon-reload
ok "systemd reloaded"

echo
echo "Note: .env, sessions/, and your MUSELAB_ROOT are NOT touched."
echo "Delete the repo to fully remove muselab."
