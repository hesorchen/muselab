#!/usr/bin/env bash
# muselab lint — catches the bug classes we've shipped historically.
# Run locally before commits; wire into CI for enforcement.
#
# Checks:
#   1. Python read_text / write_text without encoding=
#   2. .thinking class collision (mascot vs message bubble)
#   3. Personal-data / PII leak — generic patterns (constitution P6 / §6)
#   4. Maintainer-identity leak — runtime-derived ($USER, $HOME) + optional
#      private blacklist file. NO private literal is ever stored in this script.
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
  grep -rnE --include="*.py" '(^|[^A-Za-z0-9_])(read_text|write_text)[[:space:]]*\(' backend/ 2>/dev/null \
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

# Tracked text files only — this is exactly the "shipped artifacts" surface
# that constitution P6 governs. `git ls-files` naturally excludes .env /
# sessions/ (gitignored) and anything not committed. Skip vendored libs,
# lockfiles and the third-party license dump (legit external names there).
_tracked_text_files() {
  git ls-files -- \
    ':!frontend/vendor/**' ':!*.lock' ':!uv.lock' \
    ':!THIRD_PARTY_LICENSES.md' ':!frontend/assets/**' \
    ':!*.png' ':!*.jpg' ':!*.jpeg' ':!*.gif' ':!*.ico' ':!*.webp' \
    ':!*.woff' ':!*.woff2' ':!*.ttf' 2>/dev/null
}

echo "== Check 3: personal-data / PII leak (generic patterns) =="
# Generic, ship-safe patterns verified clean against the current tree. These
# carry NO private literal, so the check is identical on every machine + CI.
#   - CN mobile (1[3-9] + 9 digits)   - 18-digit national ID (17 + [0-9Xx])
pii_hits=""
while IFS= read -r f; do
  [[ -f "$f" ]] || continue
  m=$(grep -nE '\b1[3-9][0-9]{9}\b|\b[0-9]{17}[0-9Xx]\b' "$f" 2>/dev/null) || true
  [[ -n "$m" ]] && pii_hits+="$(echo "$m" | sed "s|^|$f:|")"$'\n'
done < <(_tracked_text_files)
if [[ -n "${pii_hits// /}" ]]; then
  red "FAIL — possible phone number / national ID in a tracked file:"
  echo "$pii_hits" | sed '/^$/d;s/^/  /'
  echo "  → Remove it, or if it is a deliberate fake example use an obvious"
  echo "    hyphenated placeholder (e.g. 138-XXXX-XXXX) so it cannot match a real number."
  fail=1
else
  green "OK — no phone / ID patterns in tracked files."
fi
echo

echo "== Check 4: maintainer-identity leak (runtime-derived + private blacklist) =="
# Guard: the private blacklist MUST stay untracked. If it ever gets committed,
# that itself is the leak — hard fail before scanning anything.
if git ls-files --error-unmatch scripts/.leak-blacklist >/dev/null 2>&1; then
  red "FAIL — scripts/.leak-blacklist is git-tracked. It holds private literals"
  red "        and MUST be gitignored. Run: git rm --cached scripts/.leak-blacklist"
  fail=1
fi

# Build the forbidden-literal list WITHOUT writing any private value into this
# script. Runtime-derived identities (your OS login + home dir basename) catch
# the historical `/home/<login>` class automatically, for every contributor.
declare -a forbidden=()
# Common CI / system / container account names that are NOT personal — skip so
# CI (runs as `runner`) and Docker (`muse`) don't self-trip.
_generic_account=" runner root user ubuntu admin muse you me alice bob test ci "
for id in "$(whoami 2>/dev/null)" "$(basename "${HOME:-}" 2>/dev/null)"; do
  [[ -z "$id" || ${#id} -lt 3 ]] && continue
  [[ "$_generic_account" == *" ${id} "* ]] && continue
  forbidden+=("$id")
done
# Optional private blacklist: env override, else gitignored file. One literal
# per line; blank lines and #-comments ignored. Lives only on your machine.
_bl="${MUSELAB_LEAK_BLACKLIST:-scripts/.leak-blacklist}"
if [[ -f "$_bl" ]]; then
  while IFS= read -r line; do
    line="${line%%#*}"; line="${line#"${line%%[![:space:]]*}"}"; line="${line%"${line##*[![:space:]]}"}"
    [[ -n "$line" ]] && forbidden+=("$line")
  done < "$_bl"
fi

if (( ${#forbidden[@]} == 0 )); then
  yellow "SKIP — no identity literals to check (generic account + no blacklist file)."
elif [[ ! -t 1 && -z "${MUSELAB_LEAK_BLACKLIST:-}" && ! -f scripts/.leak-blacklist ]]; then
  yellow "NOTE — only runtime-derived identities checked (no blacklist on this host)."
fi

id_hits=""
if (( ${#forbidden[@]} > 0 )); then
  # Build one alternation; grep -F-style literal match, case-insensitive.
  pat=$(printf '%s\n' "${forbidden[@]}" | sed 's/[][\.*^$/]/\\&/g' | paste -sd'|' -)
  while IFS= read -r f; do
    [[ -f "$f" ]] || continue
    m=$(grep -niE "$pat" "$f" 2>/dev/null) || true
    [[ -n "$m" ]] && id_hits+="$(echo "$m" | sed "s|^|$f:|")"$'\n'
  done < <(_tracked_text_files)
fi
if [[ -n "${id_hits// /}" ]]; then
  red "FAIL — maintainer-identity literal found in a tracked (shippable) file:"
  echo "$id_hits" | sed '/^$/d;s/^/  /'
  echo "  → constitution P6: no real personal data in shipped artifacts."
  echo "    Replace with a neutral placeholder (you / alice / \$HOME)."
  fail=1
else
  green "OK — no maintainer-identity literals in tracked files."
fi
echo

if (( fail > 0 )); then
  red "Lint FAILED with errors above."
  exit 1
fi
green "All lint checks passed."
