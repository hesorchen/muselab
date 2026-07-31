#!/usr/bin/env bash
# muselab workspace instructions — create or refresh an optional CLAUDE.md.
# The platform installers deliberately do not create CLAUDE.md or impose a
# directory taxonomy. Run this helper only when a workspace needs durable
# project instructions. Existing files are backed up before replacement.
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

# Portable .env value extraction (BSD/macOS grep lacks -P/\K).
WORKSPACE="$(grep -E '^MUSELAB_ROOT=' .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]')"
if [[ -z "$WORKSPACE" || ! -d "$WORKSPACE" ]]; then
  err "MUSELAB_ROOT in .env is missing or not a directory: '$WORKSPACE'"
  exit 1
fi

# Template language can be selected explicitly. Otherwise follow the shell
# locale. This affects only the generated CLAUDE.md, not the app UI.
MUSE_LOCALE="${MUSELAB_LOCALE:-}"
if [[ "$MUSE_LOCALE" != "zh" && "$MUSE_LOCALE" != "en" ]]; then
  if [[ "${LANG:-}${LC_ALL:-}${LC_MESSAGES:-}" == *zh* ]]; then
    MUSE_LOCALE=zh
  else
    MUSE_LOCALE=en
  fi
fi
if [[ "$MUSE_LOCALE" == "zh" ]]; then
  MUSE_CLAUDE_TPL="scripts/templates/default-CLAUDE.md"
  bold "muselab 工作区说明 — 主工作区：$WORKSPACE"
else
  MUSE_CLAUDE_TPL="scripts/templates/default-CLAUDE.en.md"
  bold "muselab workspace instructions — primary workspace: $WORKSPACE"
fi
echo

if [[ -f "$WORKSPACE/CLAUDE.md" ]]; then
  warn "$WORKSPACE/CLAUDE.md already exists"
  if [[ "$MUSE_LOCALE" == "zh" ]]; then
    PROMPT_OVERWRITE='用通用工作区模板覆盖？（旧内容会备份到 CLAUDE.md.bak） [y/N]:'
  else
    PROMPT_OVERWRITE='Replace it with the generic workspace template? (existing content goes to CLAUDE.md.bak) [y/N]:'
  fi
  REPLY="$(ask "$PROMPT_OVERWRITE" 'N')"
  if [[ ! "$REPLY" =~ ^[Yy] ]]; then
    if [[ "$MUSE_LOCALE" == "zh" ]]; then
      echo "  已取消。可以直接编辑 $WORKSPACE/CLAUDE.md。"
    else
      echo "  Aborted. Edit $WORKSPACE/CLAUDE.md directly if you only need a small change."
    fi
    exit 0
  fi
  cp "$WORKSPACE/CLAUDE.md" "$WORKSPACE/CLAUDE.md.bak"
  ok "backed up existing CLAUDE.md → CLAUDE.md.bak"
fi

echo
if [[ "$MUSE_LOCALE" == "zh" ]]; then
  echo "  --- 可选工作区信息（任意题回车跳过）---"
  WORKSPACE_PURPOSE="$(ask '这个工作区主要用于什么？' '')"
  WORKSPACE_SOURCES="$(ask '主要事实来源（文件或目录）:' '')"
  WORKSPACE_COMMANDS="$(ask '常用运行命令:' '')"
  WORKSPACE_VERIFY="$(ask '任务完成前应运行什么验证？' '')"
  WORKSPACE_OUTPUT="$(ask '产物通常写到哪里？' '')"
else
  echo "  --- Optional workspace details (press Enter to skip any) ---"
  WORKSPACE_PURPOSE="$(ask 'What is this workspace mainly for?' '')"
  WORKSPACE_SOURCES="$(ask 'Primary sources of truth (files or directories):' '')"
  WORKSPACE_COMMANDS="$(ask 'Common run commands:' '')"
  WORKSPACE_VERIFY="$(ask 'What should be verified before completion?' '')"
  WORKSPACE_OUTPUT="$(ask 'Where should artifacts usually be written?' '')"
fi

sed -e "s|%DATE%|$(date +%Y-%m-%d)|" \
  "$MUSE_CLAUDE_TPL" > "$WORKSPACE/CLAUDE.md"

_patch() {
  local label="$1" value="$2"
  [[ -z "$value" ]] && return
  awk -v lbl="$label" -v val=" $value" '
    !done && $0 == lbl { print lbl val; done=1; next } { print }
  ' "$WORKSPACE/CLAUDE.md" > "$WORKSPACE/CLAUDE.md.tmp" \
    && mv "$WORKSPACE/CLAUDE.md.tmp" "$WORKSPACE/CLAUDE.md"
}
if [[ "$MUSE_LOCALE" == "zh" ]]; then
  _patch "- 工作区用途：" "$WORKSPACE_PURPOSE"
  _patch "- 主要事实来源：" "$WORKSPACE_SOURCES"
  _patch "- 常用运行命令：" "$WORKSPACE_COMMANDS"
  _patch "- 完成前验证：" "$WORKSPACE_VERIFY"
  _patch "- 产物目录：" "$WORKSPACE_OUTPUT"
else
  _patch "- Purpose:" "$WORKSPACE_PURPOSE"
  _patch "- Primary sources of truth:" "$WORKSPACE_SOURCES"
  _patch "- Common commands:" "$WORKSPACE_COMMANDS"
  _patch "- Verification before completion:" "$WORKSPACE_VERIFY"
  _patch "- Artifact/output directory:" "$WORKSPACE_OUTPUT"
fi

ok "CLAUDE.md → $WORKSPACE/CLAUDE.md"
echo
if [[ "$MUSE_LOCALE" == "zh" ]]; then
  echo "  这是可选工作区说明；脚本没有创建或修改其他目录。"
  echo "  下一次在该工作区开始对话时会自动加载，无需重启服务。"
else
  echo "  This is an optional workspace guide; no other directories were created or changed."
  echo "  It is loaded on the next conversation in this workspace; no service restart is needed."
fi
