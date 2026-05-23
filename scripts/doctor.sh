#!/usr/bin/env bash
# muselab doctor — re-runs the same checks the installer does, plus probes a
# running instance. Use after install / after weird behaviour / before file
# bug report. Works on Linux + macOS (Windows version: doctor.ps1).
set -uo pipefail   # NOT -e: keep going past failures to give full report

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
err()  { printf "  \033[31m✗\033[0m %s\n" "$*" >&2; }
note() { printf "    %s\n" "$*"; }

FAIL=0
WARN=0

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
  TOKEN=$(grep -oP 'MUSELAB_TOKEN=\K\S+' .env 2>/dev/null || echo "")
  ROOT=$(grep -oP 'MUSELAB_ROOT=\K\S+' .env 2>/dev/null || echo "")
  PORT=$(grep -oP 'MUSELAB_PORT=\K\S+' .env 2>/dev/null || echo "8765")
  if [[ -z "$TOKEN" ]]; then err "MUSELAB_TOKEN missing in .env"; FAIL=$((FAIL+1))
  elif (( ${#TOKEN} < 16 )); then err "MUSELAB_TOKEN too short (${#TOKEN} chars; need ≥16)"; FAIL=$((FAIL+1))
  else ok "MUSELAB_TOKEN set (${TOKEN:0:4}…${TOKEN: -4}, ${#TOKEN} chars)"; fi
  if [[ -z "$ROOT" ]]; then err "MUSELAB_ROOT missing in .env"; FAIL=$((FAIL+1))
  elif [[ ! -d "$ROOT" ]]; then err "MUSELAB_ROOT=$ROOT but directory doesn't exist"; FAIL=$((FAIL+1))
  else
    ok "MUSELAB_ROOT = $ROOT"
    if [[ -f "$ROOT/CLAUDE.md" ]]; then
      L=$(wc -l < "$ROOT/CLAUDE.md")
      ok "  CLAUDE.md present ($L lines)"
    else
      warn "  no CLAUDE.md at $ROOT — Muse will use defaults; run scripts/intake.sh to add"
      WARN=$((WARN+1))
    fi
    for sub in health work money people notes archives; do
      if [[ -d "$ROOT/$sub" ]]; then ok "  $sub/ present"
      else note "  $sub/ missing (intake hasn't run, or you deleted it)"; fi
    done
  fi
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
[[ -n "${DEEPSEEK_API_KEY:-}"    ]] || DEEPSEEK_API_KEY=$(grep -oP 'DEEPSEEK_API_KEY=\K\S+' .env 2>/dev/null || echo "")
[[ -n "${ZHIPUAI_API_KEY:-}"     ]] || ZHIPUAI_API_KEY=$(grep -oP 'ZHIPUAI_API_KEY=\K\S+' .env 2>/dev/null || echo "")
[[ -n "${MINIMAX_API_KEY:-}"     ]] || MINIMAX_API_KEY=$(grep -oP 'MINIMAX_API_KEY=\K\S+' .env 2>/dev/null || echo "")
[[ -n "${MOONSHOT_API_KEY:-}"    ]] || MOONSHOT_API_KEY=$(grep -oP 'MOONSHOT_API_KEY=\K\S+' .env 2>/dev/null || echo "")
[[ -n "${DASHSCOPE_API_KEY:-}"   ]] || DASHSCOPE_API_KEY=$(grep -oP 'DASHSCOPE_API_KEY=\K\S+' .env 2>/dev/null || echo "")
[[ -n "${XIAOMI_MIMO_API_KEY:-}" ]] || XIAOMI_MIMO_API_KEY=$(grep -oP 'XIAOMI_MIMO_API_KEY=\K\S+' .env 2>/dev/null || echo "")
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
