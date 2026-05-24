#!/usr/bin/env bash
# muselab lint — catches the bug classes we've shipped historically.
# Run locally before commits; wire into CI for enforcement.
#
# Checks:
#   1. Python read_text / write_text without encoding=
#   2. .thinking class collision (mascot vs message bubble)
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

fail=0
red()   { printf '\033[31m%s\033[0m\n' "$*" >&2; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }

echo "== Check 1: Python read_text/write_text without encoding =="
# Multi-line tolerant: match the call, then look 0–3 lines for `encoding=`.
violations=$(
  grep -rnP --include="*.py" '\b(read_text|write_text)\s*\(' backend/ 2>/dev/null \
    | grep -v __pycache__ \
    | while IFS=: read -r f ln rest; do
        # Read lines f starting at $ln through $((ln+3)) and check encoding=
        if ! sed -n "${ln},$((ln+3))p" "$f" 2>/dev/null | grep -q "encoding="; then
          echo "$f:$ln: $(echo "$rest" | sed 's/^[[:space:]]*//')"
        fi
      done
)
if [[ -n "$violations" ]]; then
  red "FAIL — Python file I/O without encoding=\"utf-8\":"
  echo "$violations" | sed 's/^/  /'
  echo "  → Add encoding=\"utf-8\" to every read_text() / write_text()."
  fail=1
else
  green "OK — all Python file I/O specifies encoding."
fi
echo

echo "== Check 2: .thinking class used outside message-bubble context =="
# Mascot used to bind {'thinking': streaming} on a generic header element,
# which collided with .thinking bubble style. New mascot uses is-streaming.
violations=$(grep -nE "'thinking'\s*:" frontend/*.html frontend/*.js 2>/dev/null || true)
if [[ -n "$violations" ]]; then
  red "FAIL — .thinking class added via :class binding (may collide with bubble style):"
  echo "$violations" | sed 's/^/  /'
  echo "  → Rename to is-streaming or another state-prefixed class. See styles.css:.muse-mascot.is-streaming."
  fail=1
else
  green "OK — no .thinking class collisions detected."
fi
echo

if (( fail > 0 )); then
  red "Lint FAILED with errors above."
  exit 1
fi
green "All lint checks passed."
