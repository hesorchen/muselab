#!/usr/bin/env bash
# Install only the macOS menu-bar client for a MuseLab server hosted elsewhere.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.muselab.statusbar"
DOMAIN="gui/$(id -u)"
APP_DIR="$HOME/Library/Application Support/muselab"
LOG_DIR="$HOME/Library/Logs/muselab"
AGENT_DIR="$HOME/Library/LaunchAgents"
BINARY="$APP_DIR/MuseLabStatusBar"
CONFIG="$APP_DIR/statusbar.env"
PLIST="$AGENT_DIR/$LABEL.plist"
BUILD="$REPO/build/macos/MuseLabStatusBar"
NONINT="${MUSELAB_NONINTERACTIVE:-0}"

ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
err()  { printf "  \033[31m✗\033[0m %s\n" "$*" >&2; }

if [[ "$(uname -s)" != "Darwin" ]]; then
  err "This installer must run on the macOS client."
  exit 1
fi
if [[ $EUID -eq 0 ]]; then
  err "Don't run this with sudo; the icon is a user LaunchAgent."
  exit 1
fi
if ! command -v xcrun >/dev/null 2>&1 || ! xcrun --find swiftc >/dev/null 2>&1; then
  err "Swift compiler not found. Run: xcode-select --install"
  exit 1
fi

URL="${MUSELAB_URL:-}"
TOKEN="${MUSELAB_TOKEN:-}"
if [[ -z "$URL" && "$NONINT" != "1" ]]; then
  read -r -p "  Linux MuseLab URL (for example https://muse.example.com): " URL
fi
URL="${URL%/}"
if [[ ! "$URL" =~ ^https?://[^/?#[:space:]@]+$ ]]; then
  err "MUSELAB_URL must be an http(s) origin without a path, query, credentials, or fragment."
  exit 1
fi
if [[ "$URL" == http://* && "$URL" != http://127.0.0.1:* && "$URL" != http://localhost:* ]]; then
  warn "The token will cross the network over plain HTTP. HTTPS is strongly recommended."
fi
if [[ -z "$TOKEN" && "$NONINT" != "1" ]]; then
  read -r -s -p "  MUSELAB_TOKEN from the Linux server: " TOKEN
  printf '\n'
fi
if (( ${#TOKEN} < 16 )) || [[ "$TOKEN" == *$'\n'* || "$TOKEN" == *$'\r'* ]]; then
  err "MUSELAB_TOKEN must be at least 16 characters and fit on one line."
  exit 1
fi

printf "  Checking %s…\n" "$URL"
if ! curl -fsS --connect-timeout 5 --max-time 15 \
    -H "X-Auth-Token: $TOKEN" \
    "$URL/api/activity/summary" >/dev/null; then
  err "Could not authenticate to $URL/api/activity/summary"
  err "Check the URL, token, HTTPS certificate, firewall, and reverse proxy."
  exit 1
fi
ok "remote activity API reachable"

mkdir -p "$REPO/build/macos" "$APP_DIR" "$LOG_DIR" "$AGENT_DIR"
xcrun swiftc -O -framework AppKit -framework Foundation \
  "$REPO/macos/statusbar/main.swift" -o "$BUILD"
install -m 755 "$BUILD" "$BINARY"
(umask 077; printf "MUSELAB_URL=%s\nMUSELAB_TOKEN='%s'\n" "$URL" "$TOKEN" > "$CONFIG")
chmod 600 "$CONFIG"
ok "status bar helper installed"

xml_escape() {
  local value="$1"
  value="${value//&/&amp;}"
  value="${value//</&lt;}"
  value="${value//>/&gt;}"
  value="${value//\"/&quot;}"
  value="${value//\'/&apos;}"
  printf '%s' "$value"
}
BINARY_XML="$(xml_escape "$BINARY")"
CONFIG_XML="$(xml_escape "$CONFIG")"
LOG_DIR_XML="$(xml_escape "$LOG_DIR")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$BINARY_XML</string><string>--env</string><string>$CONFIG_XML</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG_DIR_XML/statusbar-stdout.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR_XML/statusbar-stderr.log</string>
</dict></plist>
EOF
plutil -lint "$PLIST" >/dev/null
ok "LaunchAgent plist validated"

if [[ "${MUSELAB_SKIP_SERVICE:-0}" == "1" ]]; then
  warn "LaunchAgent start skipped (MUSELAB_SKIP_SERVICE=1)"
else
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  if ! launchctl bootstrap "$DOMAIN" "$PLIST"; then
    err "LaunchAgent failed to load; inspect $LOG_DIR/statusbar-stderr.log"
    exit 1
  fi
  launchctl enable "$DOMAIN/$LABEL"
  launchctl kickstart -k "$DOMAIN/$LABEL"
  launchctl print "$DOMAIN/$LABEL" >/dev/null
  ok "menu-bar icon started"
fi

printf '\nInstalled remote MuseLab menu-bar client.\n'
printf '  Server: %s\n' "$URL"
printf '  Config: %s\n' "$CONFIG"
printf '  Restart: launchctl kickstart -k %s/%s\n' "$DOMAIN" "$LABEL"
