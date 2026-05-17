#!/usr/bin/env bash
# muselab intake — (re)run the 7-question profile setup and update CLAUDE.md.
# Use when:
#   - your first install skipped the intake (answered "n")
#   - you want to refresh the profile after life changes
#   - you cloned an existing .env from elsewhere but never set up CLAUDE.md
# Linux + macOS. Windows: scripts\intake.ps1.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
err()  { printf "  \033[31m✗\033[0m %s\n" "$*" >&2; }
ask()  { local q="$1" def="${2:-}" ans; read -rp "  $q ${def:+[$def]} " ans; echo "${ans:-$def}"; }

if [[ ! -f .env ]]; then
  err ".env not found — run scripts/install-{linux,macos}.sh first"
  exit 1
fi

ARCHIVE="$(grep -oP 'MUSELAB_ROOT=\K\S+' .env || true)"
if [[ -z "$ARCHIVE" || ! -d "$ARCHIVE" ]]; then
  err "MUSELAB_ROOT in .env is missing or not a directory: '$ARCHIVE'"
  exit 1
fi

bold "muselab intake — archive at $ARCHIVE"
echo

# Confirm overwrite if CLAUDE.md already exists
if [[ -f "$ARCHIVE/CLAUDE.md" ]]; then
  warn "$ARCHIVE/CLAUDE.md already exists"
  REPLY="$(ask 'Overwrite with a fresh template? (existing content goes to CLAUDE.md.bak) [y/N]:' 'N')"
  if [[ ! "$REPLY" =~ ^[Yy] ]]; then
    echo "  Aborted. Edit $ARCHIVE/CLAUDE.md manually if you just want to tweak it."
    exit 0
  fi
  cp "$ARCHIVE/CLAUDE.md" "$ARCHIVE/CLAUDE.md.bak"
  ok "backed up existing CLAUDE.md → CLAUDE.md.bak"
fi

# Subdirs — create only the ones missing (don't overwrite existing READMEs)
for sub in health work money people notes archives; do
  if [[ ! -d "$ARCHIVE/$sub" ]]; then
    mkdir -p "$ARCHIVE/$sub"
    cp "scripts/templates/archive-skeleton/$sub/README.md" "$ARCHIVE/$sub/README.md"
    ok "created $sub/"
  fi
done

echo
echo "  --- Quick intake (press Enter to skip any) ---"
INTAKE_NAME="$(ask 'How should Muse address you?' '')"
INTAKE_BIRTH="$(ask 'Birth year (or just an age range):' '')"
INTAKE_CITY="$(ask 'Where do you live?' '')"
INTAKE_DOING="$(ask 'What occupies most of your week? (study / job / freelance / care / retirement / …)' '')"
INTAKE_STAGE="$(ask 'One sentence about your life stage right now:' '')"
INTAKE_GOAL="$(ask 'One main goal for this year:' '')"
INTAKE_HEALTH="$(ask 'Top health concern right now (or "none"):' '')"

sed -e "s|%DATE%|$(date +%Y-%m-%d)|" \
  scripts/templates/default-CLAUDE.md > "$ARCHIVE/CLAUDE.md"

# Patch values into CLAUDE.md. Portable awk-based replace (GNU + BSD safe).
_patch() {
  local label="$1" value="$2"
  [[ -z "$value" ]] && return
  local esc
  esc="$(printf '%s' "$value" | sed -e 's/[\\&|]/\\&/g')"
  awk -v lbl="- $label：" -v val=" $esc" '
    !done && $0 == lbl { print lbl val; done=1; next } { print }
  ' "$ARCHIVE/CLAUDE.md" > "$ARCHIVE/CLAUDE.md.tmp" \
    && mv "$ARCHIVE/CLAUDE.md.tmp" "$ARCHIVE/CLAUDE.md"
}
_patch "称呼 / 名字（你希望 Muse 叫你什么）" "$INTAKE_NAME"
_patch "出生年份（年龄段就行，不必精确）"     "$INTAKE_BIRTH"
_patch "现在住在"                              "$INTAKE_CITY"
_patch "我现在主要在做"                        "$INTAKE_DOING"
_patch "这一年最想做成的一件事"               "$INTAKE_GOAL"
_patch "当前最关心的健康问题（如有）"         "$INTAKE_HEALTH"
if [[ -n "$INTAKE_STAGE" ]]; then
  STAGE_ESC="$(printf '%s' "$INTAKE_STAGE" | sed -e 's/[\\&|]/\\&/g')"
  # Portable in-place sed (GNU vs BSD)
  if sed --version >/dev/null 2>&1; then
    sed -i "s|（如：「大三在准备保研」|$STAGE_ESC\\n\\n（如：「大三在准备保研」|" "$ARCHIVE/CLAUDE.md"
  else
    sed -i '' "s|（如：「大三在准备保研」|$STAGE_ESC\\n\\n（如：「大三在准备保研」|" "$ARCHIVE/CLAUDE.md"
  fi
fi

ok "CLAUDE.md updated"
echo
echo "  Next: open $ARCHIVE/CLAUDE.md and fill any blanks. Muse picks it up on next chat (no restart)."
