#!/usr/bin/env bash
# muselab doctor — re-runs the same checks the installer does, plus probes a
# running instance. Use after install / after weird behaviour / before file
# bug report. Works on Linux + macOS + WSL2.
set -uo pipefail   # NOT -e: keep going past failures to give full report

# Bail out early on native Windows shells — this script only knows systemd
# (Linux / WSL2) and launchd (macOS). Git Bash / MSYS / Cygwin on native
# Windows would silently skip the service-status section.
case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*|Windows_NT)
    printf "\033[31m✗\033[0m This script is for Linux / macOS / WSL2 only.\n" >&2
    printf "  On Windows, install muselab inside WSL2 and run this script there.\n" >&2
    printf "  See docs/quickstart.md (Windows via WSL2).\n" >&2
    exit 1
    ;;
esac

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
err()  { printf "  \033[31m✗\033[0m %s\n" "$*" >&2; }
note() { printf "    %s\n" "$*"; }

FAIL=0
WARN=0
TOKEN=""
WORKSPACE=""
PORT="8765"

# Read a simple dotenv assignment without sourcing the file. Sourcing .env
# would execute shell syntax; doctor only needs scalar values and must remain a
# read-only diagnostic for configuration content.
env_value() {
  local key="$1" line value
  line="$(grep -E "^[[:space:]]*${key}[[:space:]]*=" .env 2>/dev/null | head -1)"
  [[ -n "$line" ]] || return 0
  value="${line#*=}"
  # Trim only the edges so paths containing spaces stay intact.
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  if (( ${#value} >= 2 )) \
      && [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
    value="${value:1:${#value}-2}"
    value="${value//\\\"/\"}"
    value="${value//\\\\/\\}"
  elif (( ${#value} >= 2 )) \
      && [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "$value"
}

probe_writable_dir() {
  local label="$1" path="$2" probe
  if [[ -e "$path" && ! -d "$path" ]]; then
    err "$label=$path exists but is not a directory"
    FAIL=$((FAIL+1))
    return
  fi
  if [[ ! -d "$path" ]]; then
    if mkdir -p "$path" 2>/dev/null; then
      ok "$label created: $path"
    else
      err "$label=$path cannot be created"
      FAIL=$((FAIL+1))
      return
    fi
  fi
  probe="$(mktemp "$path/.muselab-doctor-write.XXXXXX" 2>/dev/null)"
  if [[ -n "$probe" ]]; then
    rm -f "$probe"
    ok "$label writable: $path"
  else
    err "$label=$path is not writable"
    FAIL=$((FAIL+1))
  fi
}

bold "muselab doctor — $(date)"
echo  "  Repo: $REPO"
echo

bold "1. Prerequisites"
if command -v uv >/dev/null 2>&1; then
  ok "uv: $(uv --version 2>&1)"
else
  err "uv not found — install from https://astral.sh/uv"
  FAIL=$((FAIL+1))
fi
if command -v claude >/dev/null 2>&1; then
  ok "claude CLI: $(claude --version 2>&1 | head -1)"
  if [[ -f "$HOME/.claude/.credentials.json" ]]; then
    ok "  Pro OAuth present (~/.claude/.credentials.json)"
  else
    warn "  no Pro OAuth credentials — run 'claude login' for Anthropic models"
    WARN=$((WARN+1))
  fi
else
  warn "claude CLI missing — Anthropic models unavailable, only configured 3rd-party providers will work"
  WARN=$((WARN+1))
fi
for r in uvx npx; do
  if command -v $r >/dev/null 2>&1; then ok "$r present"
  else warn "$r missing — some MCP presets won't run"; WARN=$((WARN+1)); fi
done

echo
bold "2. Configuration"
if [[ -f .env ]]; then
  ok ".env present"
  TOKEN="$(env_value MUSELAB_TOKEN)"
  WORKSPACE="$(env_value MUSELAB_ROOT)"
  PORT="$(env_value MUSELAB_PORT)"
  PORT="${PORT:-8765}"
  if [[ -z "$TOKEN" ]]; then err "MUSELAB_TOKEN missing in .env"; FAIL=$((FAIL+1))
  elif (( ${#TOKEN} < 16 )); then err "MUSELAB_TOKEN too short (${#TOKEN} chars; need ≥16)"; FAIL=$((FAIL+1))
  else ok "MUSELAB_TOKEN set (${#TOKEN} chars; value not displayed)"; fi
  if [[ -z "$WORKSPACE" ]]; then err "MUSELAB_ROOT missing in .env"; FAIL=$((FAIL+1))
  elif [[ ! -d "$WORKSPACE" ]]; then err "MUSELAB_ROOT=$WORKSPACE but directory doesn't exist"; FAIL=$((FAIL+1))
  else
    ok "MUSELAB_ROOT = $WORKSPACE"
    probe_writable_dir "MUSELAB_ROOT" "$WORKSPACE"
    if [[ -f "$WORKSPACE/CLAUDE.md" ]]; then
      L=$(wc -l < "$WORKSPACE/CLAUDE.md")
      ok "  CLAUDE.md present ($L lines)"
    else
      note "CLAUDE.md not present (optional; run scripts/intake.sh to add)"
    fi
  fi

  SESSIONS_DIR="$(env_value MUSELAB_SESSIONS_DIR)"
  if [[ -z "$SESSIONS_DIR" ]]; then
    SESSIONS_DIR="$REPO/sessions"
    note "MUSELAB_SESSIONS_DIR unset; using default: $SESSIONS_DIR"
  elif [[ "$SESSIONS_DIR" != /* ]]; then
    SESSIONS_DIR="$REPO/$SESSIONS_DIR"
  fi
  probe_writable_dir "MUSELAB_SESSIONS_DIR" "$SESSIONS_DIR"
else
  err ".env not found — run scripts/install-{linux,macos}.sh first"
  FAIL=$((FAIL+1))
fi

echo
bold "3. Python deps"
if uv sync --frozen --no-progress >/dev/null 2>&1; then
  ok "uv sync --frozen passes"
elif uv sync --no-progress >/dev/null 2>&1; then
  warn "uv.lock out of sync — re-run scripts/install-{linux,macos}.sh"
  WARN=$((WARN+1))
else
  err "uv sync failed — see: uv sync"
  FAIL=$((FAIL+1))
fi

echo
bold "4. Service"
if command -v systemctl >/dev/null 2>&1; then
  if systemctl --user is-active --quiet muselab.service 2>/dev/null; then
    ok "systemd: muselab.service active"
  elif systemctl --user list-unit-files muselab.service >/dev/null 2>&1; then
    warn "systemd: muselab.service registered but not active"
    note "  start: systemctl --user start muselab"
    note "  logs:  journalctl --user -u muselab -n 50"
    WARN=$((WARN+1))
  else
    warn "systemd: muselab.service not installed — run scripts/install-linux.sh"
    WARN=$((WARN+1))
  fi
elif [[ -f "$HOME/Library/LaunchAgents/com.muselab.plist" ]]; then
  if launchctl list 2>/dev/null | grep -q com.muselab; then
    ok "launchd: com.muselab loaded"
  else
    warn "launchd: plist present but agent not loaded"
    note "  load: launchctl load -w ~/Library/LaunchAgents/com.muselab.plist"
    WARN=$((WARN+1))
  fi
else
  note "no auto-start configured (you may be running uv run manually)"
fi

echo
bold "5. HTTP probe"
URL="http://127.0.0.1:${PORT:-8765}/"
HEALTH_URL="http://127.0.0.1:${PORT:-8765}/api/health"
# Prefer the auth-free /api/health (added 2026-05-20) since it returns a
# stable JSON probe shape; fall back to "/" for older builds.
if curl -fs -m 3 "$HEALTH_URL" 2>/dev/null | grep -q '"status":"ok"'; then
  ok "$HEALTH_URL responds ok"
elif curl -fs -o /dev/null -m 3 "$URL" 2>/dev/null; then
  ok "$URL responds 200 (no /api/health — older build?)"
else
  warn "$URL not responding — service may not be up"
  WARN=$((WARN+1))
fi
if [[ -n "$TOKEN" ]]; then
  if curl -fs -m 3 -H "X-Auth-Token: $TOKEN" "http://127.0.0.1:${PORT:-8765}/api/chat/context-info" >/dev/null 2>&1; then
    ok "token works — /api/chat/context-info OK"
  else
    err "token rejected by /api/chat/context-info — TOKEN in .env doesn't match running process"
    FAIL=$((FAIL+1))
  fi
fi

echo
bold "6. Provider keys"
# Portable .env value extraction (BSD/macOS grep lacks -P/\K).
_env_val() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]'; }
[[ -n "${DEEPSEEK_API_KEY:-}"    ]] || DEEPSEEK_API_KEY=$(_env_val DEEPSEEK_API_KEY)
[[ -n "${ZHIPUAI_API_KEY:-}"     ]] || ZHIPUAI_API_KEY=$(_env_val ZHIPUAI_API_KEY)
[[ -n "${MINIMAX_API_KEY:-}"     ]] || MINIMAX_API_KEY=$(_env_val MINIMAX_API_KEY)
[[ -n "${MOONSHOT_API_KEY:-}"    ]] || MOONSHOT_API_KEY=$(_env_val MOONSHOT_API_KEY)
[[ -n "${DASHSCOPE_API_KEY:-}"   ]] || DASHSCOPE_API_KEY=$(_env_val DASHSCOPE_API_KEY)
[[ -n "${XIAOMI_MIMO_API_KEY:-}" ]] || XIAOMI_MIMO_API_KEY=$(_env_val XIAOMI_MIMO_API_KEY)
for entry in \
    "DEEPSEEK_API_KEY:DeepSeek" \
    "ZHIPUAI_API_KEY:GLM" \
    "MINIMAX_API_KEY:MiniMax" \
    "MOONSHOT_API_KEY:Kimi" \
    "DASHSCOPE_API_KEY:Qwen" \
    "XIAOMI_MIMO_API_KEY:Xiaomi MiMo"; do
  envk="${entry%%:*}"; name="${entry##*:}"
  val="${!envk:-}"
  if [[ -n "$val" ]]; then
    ok "$name key configured (${val:0:4}…${val: -4})"
  else
    note "$name key not set (optional — only needed if you want to use $name models)"
  fi
done

echo
bold "Summary"
echo "  Failures: $FAIL    Warnings: $WARN"
if (( FAIL > 0 )); then
  err "doctor found blocking problems — see above"
  exit 1
elif (( WARN > 0 )); then
  warn "doctor finished with warnings"
  exit 0
else
  ok "all checks passed"
  exit 0
fi
