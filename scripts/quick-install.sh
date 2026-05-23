#!/usr/bin/env bash
# muselab — one-line bootstrap installer
#
# Designed to be piped from curl:
#   curl -fsSL https://raw.githubusercontent.com/hesorchen/muselab/main/scripts/quick-install.sh | bash
#
# What it does:
#   1. Detects OS (Linux + WSL / macOS — other platforms refuse)
#   2. Installs `uv` (the only Python prerequisite) if missing
#   3. Clones https://github.com/hesorchen/muselab → ~/muselab
#      (or prompts for a different dir; refuses to clobber an existing
#      checkout without consent)
#   4. Hands off to scripts/install-{linux,macos}.sh — those scripts know
#      how to register the systemd-user / launchd service, create .env,
#      pick a port, and so on. We don't duplicate their logic here.
#
# Why a separate bootstrap and not just curl|bash → install-linux.sh:
#   install-linux.sh runs FROM INSIDE the repo (it does `cd "$(dirname …)/.."`),
#   which assumes the repo is already cloned. Piping it directly would land
#   in /dev/stdin and fail with "scripts directory not found". This script
#   wraps clone + exec so users get one command.
#
# stdin tracking: when piped from curl, our own stdin is the pipe — `read`
# won't work. We re-attach /dev/tty for our interactive prompts AND for the
# underlying platform installer (which also uses `read`).

set -euo pipefail

REPO_URL="https://github.com/hesorchen/muselab"
DEFAULT_DEST="$HOME/muselab"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
err()  { printf "  \033[31m✗\033[0m %s\n" "$*" >&2; }

# Ask helper that reads from /dev/tty even when our stdin is a pipe.
# Returns the user's answer or the default if they hit Enter.
ask_tty() {
  local q="$1" def="${2:-}" ans
  if [[ -t 0 ]] || [[ -c /dev/tty ]]; then
    read -rp "  $q ${def:+[$def]} " ans </dev/tty
  else
    err "interactive prompts need a terminal — this script is being run"
    err "  in a non-interactive shell with no /dev/tty. Try:"
    err "    git clone $REPO_URL && cd muselab && bash scripts/install-linux.sh"
    exit 1
  fi
  echo "${ans:-$def}"
}

bold "muselab — one-line bootstrap"
echo

# ----- 1. Refuse root --------------------------------------------------------
if [[ $EUID -eq 0 ]]; then
  err "Don't run as root / with sudo."
  err "  muselab installs under your normal user (systemd --user / launchctl)."
  exit 1
fi

# ----- 2. Detect OS ----------------------------------------------------------
OS=""
case "$(uname -s)" in
  Linux*)   OS="linux" ;;
  Darwin*)  OS="macos" ;;
  *)        err "Unsupported OS: $(uname -s). muselab supports Linux + macOS."
            err "  On Windows: use WSL2 + Ubuntu and re-run this command inside WSL."
            exit 1 ;;
esac
ok "OS: $OS"

# WSL note (informational, doesn't change behaviour) — install-linux.sh
# requires systemd, which WSL2 only has when /etc/wsl.conf has
# `[boot]\nsystemd=true` set + a `wsl --shutdown` afterwards.
if [[ "$OS" == "linux" ]] && grep -qi "microsoft" /proc/version 2>/dev/null; then
  warn "WSL detected — install-linux.sh needs systemd-user."
  warn "  If install fails with 'systemctl not connected', enable systemd:"
  warn "    sudo tee /etc/wsl.conf >/dev/null <<<\$'[boot]\\nsystemd=true'"
  warn "    then  wsl --shutdown  (from PowerShell) and re-enter WSL."
fi

# ----- 3. Check git + curl (curl piped this script, so it must exist) -------
for tool in git curl; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    err "$tool not found — please install via your distro's package manager:"
    case "$OS" in
      linux) err "  Debian/Ubuntu:  sudo apt install $tool"
             err "  CentOS/RHEL:    sudo dnf install $tool" ;;
      macos) err "  macOS:          brew install $tool" ;;
    esac
    exit 1
  fi
done
ok "git + curl present"

# ----- 4. Install uv if missing ---------------------------------------------
if command -v uv >/dev/null 2>&1; then
  ok "uv already installed: $(uv --version 2>&1)"
else
  bold "Installing uv (Python project manager — the only Python dep needed)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv's installer adds ~/.local/bin to PATH via shell rc, but we're in a
  # fresh `sh` subshell that won't re-read rc. Add it for the rest of this
  # script so the upcoming `uv sync` (inside install-linux.sh) finds it.
  if [[ -d "$HOME/.local/bin" ]] && [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    export PATH="$HOME/.local/bin:$PATH"
  fi
  if [[ -d "$HOME/.cargo/bin" ]] && [[ ":$PATH:" != *":$HOME/.cargo/bin:"* ]]; then
    export PATH="$HOME/.cargo/bin:$PATH"   # uv historically installed here
  fi
  if command -v uv >/dev/null 2>&1; then
    ok "uv installed: $(uv --version 2>&1)"
  else
    err "uv install ran but the binary isn't on PATH yet. Open a new"
    err "  terminal (so your shell re-sources ~/.bashrc) and re-run this command."
    exit 1
  fi
fi

# ----- 5. Pick clone destination ---------------------------------------------
echo
bold "Where to install muselab?"
DEST="$(ask_tty "Clone destination" "$DEFAULT_DEST")"
# Expand ~ manually since `read` doesn't.
DEST="${DEST/#\~/$HOME}"

if [[ -d "$DEST" ]]; then
  if [[ -d "$DEST/.git" ]] && [[ -f "$DEST/scripts/install-linux.sh" ]]; then
    warn "$DEST already looks like a muselab checkout."
    answer="$(ask_tty "Re-use it (skip clone)? y/N" "n")"
    case "$answer" in
      y|Y|yes) ok "Using existing checkout at $DEST" ;;
      *)       err "Aborted — point this script at a different dir, or"
               err "  remove $DEST first."
               exit 1 ;;
    esac
  else
    err "$DEST exists and isn't a muselab checkout. Pick a different path."
    exit 1
  fi
else
  bold "Cloning $REPO_URL → $DEST"
  git clone --depth 1 "$REPO_URL" "$DEST"
  ok "Cloned"
fi

# ----- 6. Hand off to the platform installer --------------------------------
echo
bold "Handing off to scripts/install-$OS.sh"
echo
cd "$DEST"
# Re-attach /dev/tty so the platform installer's `read` prompts work even
# when WE were invoked via curl|bash.
if [[ -c /dev/tty ]]; then
  exec bash "scripts/install-$OS.sh" </dev/tty
else
  exec bash "scripts/install-$OS.sh"
fi
