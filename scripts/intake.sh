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

bold "muselab intake / 入门问答 — archive at $ARCHIVE"
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
echo "  --- Quick intake / 入门问答 (press Enter to skip any / 任意题回车跳过) ---"
INTAKE_NAME="$(ask 'How should Muse address you? / Muse 该怎么称呼你？' '')"
INTAKE_BIRTH="$(ask 'Birth year (or age range) / 出生年份（或大致年龄段）:' '')"
INTAKE_CITY="$(ask 'Where do you live? / 你现在住在哪？' '')"
echo "  What occupies most of your week? (study / job / freelance / care / retirement / …)"
echo "  这一周你的主要时间花在哪？（学业 / 工作 / 自由职业 / 照护家人 / 退休 / 其他）"
INTAKE_DOING="$(ask '' '')"
echo "  One sentence about your life stage right now"
echo "  用一句话描述你当下的人生阶段"
INTAKE_STAGE="$(ask '' '')"
INTAKE_GOAL="$(ask 'One main goal for this year / 这一年最想做成的一件事:' '')"
INTAKE_HEALTH="$(ask 'Top health concern right now (or "none") / 当前最关心的健康问题（无则填 none）:' '')"

sed -e "s|%DATE%|$(date +%Y-%m-%d)|" \
  scripts/templates/default-CLAUDE.md > "$ARCHIVE/CLAUDE.md"

# Patch values into CLAUDE.md. Portable awk-based replace (GNU + BSD safe).
_patch() {
  local label="$1" value="$2"
  [[ -z "$value" ]] && return
  local esc
  esc="$(printf '%s' "$value" | sed -e 's/[\\&|]/\\&/g')"
  # ${label} / ${esc} braces required — bash 3.2 under `set -u` mis-parses
  # "$label：" as one identifier when fullwidth colon UTF-8 bytes follow.
  awk -v lbl="- ${label}：" -v val=" ${esc}" '
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

ok "CLAUDE.md updated / 已更新"
echo
echo "  Next / 下一步: open $ARCHIVE/CLAUDE.md and fill any blanks."
echo "  打开上面那个文件把空字段填完。Muse 下一次 chat 会自动加载（不用重启服务）。"
