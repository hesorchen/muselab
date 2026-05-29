#!/usr/bin/env bash
# muselab intake — (re)run the 7-question profile setup and update CLAUDE.md.
# Use when:
#   - your first install skipped the intake (answered "n")
#   - you want to refresh the profile after life changes
#   - you cloned an existing .env from elsewhere but never set up CLAUDE.md
# Linux + macOS + WSL2.
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

# Locale detection — Chinese template if shell locale is zh, else English.
if [[ "${LANG:-}${LC_ALL:-}${LC_MESSAGES:-}" == *zh* ]]; then
  MUSE_LOCALE=zh
  MUSE_CLAUDE_TPL="scripts/templates/default-CLAUDE.md"
  MUSE_README_SRC="README.md"
else
  MUSE_LOCALE=en
  MUSE_CLAUDE_TPL="scripts/templates/default-CLAUDE.en.md"
  MUSE_README_SRC="README.en.md"
fi

if [[ "$MUSE_LOCALE" == "zh" ]]; then
  bold "muselab 入门问答 — archive 在 $ARCHIVE"
else
  bold "muselab intake — archive at $ARCHIVE"
fi
echo

# Confirm overwrite if CLAUDE.md already exists
if [[ -f "$ARCHIVE/CLAUDE.md" ]]; then
  warn "$ARCHIVE/CLAUDE.md already exists"
  if [[ "$MUSE_LOCALE" == "zh" ]]; then
    PROMPT_OVERWRITE='覆盖为新模板？（旧内容会备份到 CLAUDE.md.bak） [y/N]:'
  else
    PROMPT_OVERWRITE='Overwrite with a fresh template? (existing content goes to CLAUDE.md.bak) [y/N]:'
  fi
  REPLY="$(ask "$PROMPT_OVERWRITE" 'N')"
  if [[ ! "$REPLY" =~ ^[Yy] ]]; then
    if [[ "$MUSE_LOCALE" == "zh" ]]; then
      echo "  已取消。如果只是想小改，直接编辑 $ARCHIVE/CLAUDE.md。"
    else
      echo "  Aborted. Edit $ARCHIVE/CLAUDE.md manually if you just want to tweak it."
    fi
    exit 0
  fi
  cp "$ARCHIVE/CLAUDE.md" "$ARCHIVE/CLAUDE.md.bak"
  ok "backed up existing CLAUDE.md → CLAUDE.md.bak"
fi

# Subdirs — create only the ones missing (don't overwrite existing READMEs)
for sub in health work money people notes archives; do
  if [[ ! -d "$ARCHIVE/$sub" ]]; then
    mkdir -p "$ARCHIVE/$sub"
    cp "scripts/templates/archive-skeleton/$sub/$MUSE_README_SRC" "$ARCHIVE/$sub/README.md"
    ok "created $sub/"
  fi
done

echo
if [[ "$MUSE_LOCALE" == "zh" ]]; then
  echo "  --- 入门问答（任意题回车跳过）---"
  INTAKE_NAME="$(ask 'Muse 该怎么称呼你？' '')"
  INTAKE_BIRTH="$(ask '出生年份（或大致年龄段）:' '')"
  INTAKE_CITY="$(ask '你现在住在哪？' '')"
  echo "  这一周你的主要时间花在哪？（学业 / 工作 / 自由职业 / 照护家人 / 退休 / 其他）"
  INTAKE_DOING="$(ask '' '')"
  echo "  用一句话描述你当下的人生阶段"
  INTAKE_STAGE="$(ask '' '')"
  INTAKE_GOAL="$(ask '这一年最想做成的一件事:' '')"
  INTAKE_HEALTH="$(ask '当前最关心的健康问题（无则填 none）:' '')"
else
  echo "  --- Quick intake (press Enter to skip any) ---"
  INTAKE_NAME="$(ask 'How should Muse address you?' '')"
  INTAKE_BIRTH="$(ask 'Birth year (or age range):' '')"
  INTAKE_CITY="$(ask 'Where do you live?' '')"
  echo "  What occupies most of your week? (study / job / freelance / care / retirement / …)"
  INTAKE_DOING="$(ask '' '')"
  echo "  One sentence about your life stage right now"
  INTAKE_STAGE="$(ask '' '')"
  INTAKE_GOAL="$(ask 'One main goal for this year:' '')"
  INTAKE_HEALTH="$(ask 'Top health concern right now (or "none"):' '')"
fi

sed -e "s|%DATE%|$(date +%Y-%m-%d)|" \
  "$MUSE_CLAUDE_TPL" > "$ARCHIVE/CLAUDE.md"

# Patch with whole-line awk equality — robust against any chars in the
# label (slashes, parens, full-width punctuation).
_patch() {
  local label="$1" value="$2"
  [[ -z "$value" ]] && return
  awk -v lbl="$label" -v val=" $value" '
    !done && $0 == lbl { print lbl val; done=1; next } { print }
  ' "$ARCHIVE/CLAUDE.md" > "$ARCHIVE/CLAUDE.md.tmp" \
    && mv "$ARCHIVE/CLAUDE.md.tmp" "$ARCHIVE/CLAUDE.md"
}
if [[ "$MUSE_LOCALE" == "zh" ]]; then
  _patch "- 称呼："                     "$INTAKE_NAME"
  _patch "- 出生年份："                 "$INTAKE_BIRTH"
  _patch "- 现在住在："                 "$INTAKE_CITY"
  _patch "- 一句话当前人生阶段："       "$INTAKE_STAGE"
  _patch "- 主要在做："                 "$INTAKE_DOING"
  _patch "- 这一年最想做成的一件事："   "$INTAKE_GOAL"
  _patch "- 当前最关心的健康问题："     "$INTAKE_HEALTH"
else
  _patch "- Name:"                      "$INTAKE_NAME"
  _patch "- Birth year:"                "$INTAKE_BIRTH"
  _patch "- Lives in:"                  "$INTAKE_CITY"
  _patch "- Life stage (one line):"     "$INTAKE_STAGE"
  _patch "- Main activity:"             "$INTAKE_DOING"
  _patch "- Main goal this year:"       "$INTAKE_GOAL"
  _patch "- Top health concern:"        "$INTAKE_HEALTH"
fi

ok "CLAUDE.md updated"
echo
if [[ "$MUSE_LOCALE" == "zh" ]]; then
  echo "  下一步: 打开 $ARCHIVE/CLAUDE.md 把空字段填完。"
  echo "  Muse 下一次 chat 会自动加载（不用重启服务）。"
else
  echo "  Next: open $ARCHIVE/CLAUDE.md and fill in the blanks."
  echo "  Muse picks it up on the next chat — no restart needed."
fi
