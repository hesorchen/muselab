#!/usr/bin/env bash
# muselab upgrade — bump claude-agent-sdk (Python) + claude CLI (npm) to latest,
# update uv.lock, restart the service.
#
# Conservative by design:
#   - Always re-runs tests before declaring success (catches SDK API breaks)
#   - Doesn't auto-commit — leaves the diff in your working tree for review
#   - Service restart is the user's choice (commented hint at the end)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()    { printf '\033[32m  [+] %s\033[0m\n' "$*"; }
warn()  { printf '\033[33m  [!] %s\033[0m\n' "$*"; }
err()   { printf '\033[31m  [x] %s\033[0m\n' "$*" >&2; }

bold "muselab upgrade — Python SDK + claude CLI"

# ----- Current versions ---------------------------------------------------
bold "Current versions"
CUR_SDK="$(uv pip show claude-agent-sdk 2>/dev/null | awk '/^Version:/{print $2}')"
ok "claude-agent-sdk:  ${CUR_SDK:-(not installed)}"
if command -v claude >/dev/null 2>&1; then
  CUR_CLI="$(claude --version 2>/dev/null | head -1)"
  ok "claude CLI:        ${CUR_CLI:-(unknown)}"
else
  warn "claude CLI not installed — skip CLI upgrade. Install: npm i -g @anthropic-ai/claude-code"
fi

# ----- Bump SDK -----------------------------------------------------------
bold "Bumping claude-agent-sdk (uv lock --upgrade-package)"
if ! uv lock --upgrade-package claude-agent-sdk; then
  err "uv lock failed — aborting"
  exit 1
fi
uv sync --frozen
NEW_SDK="$(uv pip show claude-agent-sdk 2>/dev/null | awk '/^Version:/{print $2}')"
if [[ "$CUR_SDK" == "$NEW_SDK" ]]; then
  ok "claude-agent-sdk already at latest ($NEW_SDK)"
else
  ok "claude-agent-sdk: $CUR_SDK → $NEW_SDK"
fi

# ----- Bump CLI -----------------------------------------------------------
if command -v npm >/dev/null 2>&1; then
  bold "Bumping claude CLI"
  if npm install -g @anthropic-ai/claude-code@latest 2>&1 | tail -5; then
    NEW_CLI="$(claude --version 2>/dev/null | head -1)"
    ok "claude CLI:        ${CUR_CLI:-?} → ${NEW_CLI:-?}"
  else
    warn "npm install failed — claude CLI unchanged"
  fi
else
  warn "npm not installed — CLI upgrade skipped"
fi

# ----- Smoke test ---------------------------------------------------------
bold "Running tests to catch SDK API breaks"
if uv run pytest tests/ -q 2>&1 | tail -3; then
  ok "tests pass against new SDK"
else
  err "tests FAILED — rollback recommended: git checkout uv.lock pyproject.toml && uv sync"
  exit 1
fi

# ----- Done ---------------------------------------------------------------
echo
bold "✓ upgrade complete"
echo "  Review the lock diff:    git diff uv.lock"
echo "  Commit if happy:         git add uv.lock pyproject.toml && git commit -m 'deps: bump SDK'"
echo "  Restart service (Linux): systemctl --user restart muselab"
echo "  Restart service (macOS): launchctl kickstart -k gui/\$UID/com.muselab"
