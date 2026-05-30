#!/usr/bin/env bash
# muselab — add HTTPS reverse proxy via Caddy in front of an existing install.
#
# Prereq:
#   1. Linux VPS with muselab already installed via scripts/install-linux.sh
#      (service running, listening on 127.0.0.1:PORT).
#   2. A domain name with an A record pointing at this VPS's public IP.
#
# After this script:
#   - Caddy fetches a Let's Encrypt cert (10-60s).
#   - https://<your-host>/  serves muselab over HTTPS.
#   - The token requirement is unchanged.
#   - Plain HTTP requests auto-redirect to HTTPS.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '\033[32m  [+] %s\033[0m\n' "$*"; }
warn() { printf '\033[33m  [!] %s\033[0m\n' "$*"; }
err()  { printf '\033[31m  [x] %s\033[0m\n' "$*" >&2; }
ask()  {
  # Use a local var (not the global $REPLY) so callers that themselves read
  # into $REPLY after calling ask() don't get clobbered.
  local q="$1" def="${2:-}" ans
  local prompt
  if [[ -n "$def" ]]; then prompt="$q [$def]: "; else prompt="$q: "; fi
  read -r -p "$prompt" ans
  echo "${ans:-$def}"
}

bold "muselab — VPS HTTPS setup via Caddy"

# This script is Linux-only (apt/Caddy/systemd/GNU sed -i). Guard up front so
# a macOS user doesn't hit a cryptic GNU `sed -i` / apt-get failure mid-run.
if [[ "$(uname -s)" != "Linux" ]]; then
  err "setup-https.sh is Linux-only (apt + Caddy + systemd). Detected: $(uname -s)."
  err "  On macOS, put muselab behind your own reverse proxy / tunnel instead."
  exit 1
fi

# ----- 1. Verify prereqs ---------------------------------------------------
if [[ ! -f .env ]]; then
  err "no .env at $ROOT — run scripts/install-linux.sh first."
  exit 1
fi
PORT="$(grep -E '^MUSELAB_PORT=' .env 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"
PORT="${PORT:-8765}"
HOSTBIND="$(grep -E '^MUSELAB_HOST=' .env 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"
HOSTBIND="${HOSTBIND:-127.0.0.1}"
ok "muselab binds to ${HOSTBIND}:${PORT}"

if [[ "$HOSTBIND" != "127.0.0.1" && "$HOSTBIND" != "localhost" ]]; then
  warn "muselab is bound to $HOSTBIND (not 127.0.0.1)"
  warn "  With Caddy in front you should bind muselab to 127.0.0.1 only —"
  warn "  otherwise the upstream HTTP port stays publicly reachable bypassing TLS."
  REPLY="$(ask 'Switch MUSELAB_HOST to 127.0.0.1 in .env now? [Y/n]' 'Y')"
  if [[ "$REPLY" =~ ^[Yy] ]]; then
    sed -i 's/^MUSELAB_HOST=.*/MUSELAB_HOST=127.0.0.1/' .env
    ok "MUSELAB_HOST=127.0.0.1 in .env (restart service to apply)"
    if systemctl --user is-active --quiet muselab.service 2>/dev/null; then
      systemctl --user restart muselab.service
      ok "muselab restarted on 127.0.0.1:${PORT}"
    fi
  fi
fi

# ----- 2. Hostname ---------------------------------------------------------
echo
echo "  Domain  → set an A record (host → this VPS public IP) BEFORE proceeding."
echo "  域名    → 先把 A 记录指向本 VPS 公网 IP，再继续。"
HOST="$(ask 'Hostname for muselab (e.g. muselab.example.com)')"
if [[ -z "$HOST" ]]; then err "hostname required"; exit 1; fi

# Quick DNS sanity (informational only — doesn't block)
if command -v dig >/dev/null 2>&1; then
  RESOLVED="$(dig +short "$HOST" A | head -1 || true)"
  VPS_IP="$(curl -s --max-time 3 https://api.ipify.org 2>/dev/null || true)"
  if [[ -n "$RESOLVED" && -n "$VPS_IP" && "$RESOLVED" != "$VPS_IP" ]]; then
    warn "DNS says $HOST → $RESOLVED, but VPS IP is $VPS_IP"
    warn "  Let's Encrypt will fail until DNS propagates. Continue anyway? [y/N]"
    read -r REPLY
    [[ "$REPLY" =~ ^[Yy] ]] || exit 1
  elif [[ -n "$RESOLVED" ]]; then
    ok "DNS: $HOST → $RESOLVED ✓"
  fi
fi

# ----- 3. Install Caddy ----------------------------------------------------
if ! command -v caddy >/dev/null 2>&1; then
  bold "Installing Caddy"
  sudo apt-get update -qq
  sudo apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  # --fail (the `f` in -1sLf) makes curl exit non-zero on HTTP errors so a
  # 404/HTML error page can't be piped into gpg as a bogus "key". With
  # pipefail set, a curl failure aborts the whole pipeline. Verify the
  # dearmored keyring is non-empty before trusting it.
  KEYRING=/usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o "$KEYRING"
  if ! sudo test -s "$KEYRING"; then
    err "Caddy GPG key download/dearmor produced an empty keyring — aborting."
    err "  Check network access to dl.cloudsmith.io and retry."
    sudo rm -f "$KEYRING"
    exit 1
  fi
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y caddy
fi
ok "caddy: $(caddy version | head -1)"

# ----- 4. Write Caddy snippet ---------------------------------------------
SNIPPET="/etc/caddy/conf.d/muselab.caddy"
sudo mkdir -p /etc/caddy/conf.d
sudo tee "$SNIPPET" > /dev/null <<EOF
# Generated by scripts/setup-https.sh
${HOST} {
  reverse_proxy localhost:${PORT} {
    # SSE: flush each chunk to the client immediately so chat streaming
    # doesn't get buffered at the proxy.
    flush_interval -1
    # Tool-use JSON can carry big payloads (file contents); bump headers.
    header_up X-Forwarded-Host {host}
  }
  # HSTS — once it's working, browsers stick to HTTPS for a year.
  header Strict-Transport-Security "max-age=31536000; includeSubDomains"
  # Standard hardening
  header X-Content-Type-Options nosniff
  header Referrer-Policy strict-origin-when-cross-origin
  encode gzip
}
EOF
ok "wrote $SNIPPET"

# Ensure main Caddyfile imports conf.d. Match the exact import directive (not a
# fuzzy "conf.d" substring, which a comment mentioning conf.d would falsely hit).
IMPORT_LINE="import /etc/caddy/conf.d/*.caddy"
if ! sudo grep -qxF "$IMPORT_LINE" /etc/caddy/Caddyfile 2>/dev/null; then
  echo "$IMPORT_LINE" | sudo tee -a /etc/caddy/Caddyfile > /dev/null
  ok "added 'import conf.d' to /etc/caddy/Caddyfile"
fi

# ----- 5. Firewall ---------------------------------------------------------
if command -v ufw >/dev/null 2>&1; then
  if sudo ufw status | grep -q "Status: active"; then
    sudo ufw allow 80/tcp  > /dev/null
    sudo ufw allow 443/tcp > /dev/null
    ok "ufw: 80/443 allowed"
    # Tighten: deny the muselab port from public so it's only reachable via Caddy
    sudo ufw deny "$PORT/tcp" 2>/dev/null || true
  else
    warn "ufw inactive — make sure ports 80,443 are open in your VPS firewall (cloud panel)"
  fi
else
  warn "no ufw — make sure ports 80,443 are open in your VPS firewall"
fi

# ----- 6. Reload + verify --------------------------------------------------
sudo systemctl reload caddy
ok "caddy reloaded — fetching TLS cert from Let's Encrypt (10-60s)"

echo
bold "✓ HTTPS setup complete"
echo "  Test in 10-60s:    https://${HOST}"
echo "  Watch Caddy logs:  sudo journalctl -u caddy -f"
echo "  Cert renews automatically every ~60 days."
echo
echo "  Your login token is unchanged:"
echo "    grep MUSELAB_TOKEN .env"
echo
echo "  If browser shows cert error → wait 30s and retry (Let's Encrypt sometimes"
echo "  needs a moment), or check 'sudo journalctl -u caddy --no-pager | tail -50'."
