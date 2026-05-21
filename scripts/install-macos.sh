#!/usr/bin/env bash
# muselab — one-shot macOS installer (user-level LaunchAgent)
# Usage: bash scripts/install-macos.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
err()  { printf "  \033[31m✗\033[0m %s\n" "$*" >&2; }
ask()  { local q="$1" def="${2:-}" ans; read -rp "  $q ${def:+[$def]} " ans; echo "${ans:-$def}"; }

bold "muselab — macOS installer"
echo  "  Repo: $REPO"
echo

# ----- 1. Prerequisites ---------------------------------------------------
bold "1/5  Checking prerequisites"
if [[ "$(uname -s)" != "Darwin" ]]; then
  err "This script is for macOS only. On Linux use install-linux.sh"
  exit 1
fi

# sudo refusal — LaunchAgent goes under your normal user
if [[ $EUID -eq 0 ]]; then
  err "Don't run this with sudo / as root."
  echo "      muselab runs as a user LaunchAgent (no root needed)."
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  err "uv not found. Install it first:"
  echo "      curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "      # or:  brew install uv"
  exit 1
fi
UV="$(command -v uv)"
ok "uv: $UV"

# Python 3.12+ — uv will download if missing
PYV="$(python3 --version 2>/dev/null | awk '{print $2}' || echo "")"
if [[ -z "$PYV" ]] || ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)' 2>/dev/null; then
  warn "system python is < 3.12 (or missing). uv will download Python 3.12 during sync (~50MB extra)."
fi

# Port pick + conflict check now happen at the .env step after the user can
# choose a non-default port.

if ! command -v claude >/dev/null 2>&1; then
  warn "claude CLI not found. Anthropic models won't work until you install it"
  warn "  and run 'claude login'. Non-Claude providers will still work via API keys."
else
  ok "claude CLI: $(command -v claude)"
  if [[ ! -d "$HOME/.claude" ]]; then
    warn "  ~/.claude not found — run 'claude login' before installing for OAuth to work"
  fi
fi

# MCP runtimes — non-fatal but warn.
if command -v uvx >/dev/null 2>&1; then
  ok "uvx present — uv-based MCP servers (fetch, git, time, …) available"
else
  warn "uvx not found — uv-based MCP presets (fetch, git, time) won't run"
fi
if command -v npx >/dev/null 2>&1; then
  ok "npx present — npm-based MCP servers (memory, sequential-thinking, …) available"
else
  warn "npx not found — npm-based MCP presets (memory, sequential-thinking, filesystem) won't run"
  warn "  install: brew install node"
fi

# ----- 2. Python deps ----------------------------------------------------
bold "2/5  Installing Python dependencies / 安装 Python 依赖 (uv sync, may take a few minutes first time)"
uv sync                              # no --quiet: user wants to see progress
ok "deps installed"

# ----- 3. .env -----------------------------------------------------------
bold "3/5  Configuring .env / 写入 .env 配置"
if [[ -f .env ]]; then
  ok ".env already exists — keeping it as is"
else
  # Token: random 64-hex (256-bit) by default; user can pick a memorable
  # password instead (>= 16 chars — backend rejects shorter).
  echo
  echo "  Login token = your password for the web UI. Stored in .env + browser localStorage."
  echo "  登录口令 = 浏览器登录用的密码。会写进 .env，浏览器记住后不用反复输入。"
  echo "  Press Enter to auto-generate a 64-char random token (recommended)."
  echo "  直接回车 = 随机生成 64 位（推荐）；想自己设密码就直接输入（≥16 字符）。"
  while true; do
    read -r -p "  Token (Enter for random / 回车随机): " TOKEN_INPUT
    if [[ -z "$TOKEN_INPUT" ]]; then
      TOKEN="$(openssl rand -hex 32)"
      ok "auto-generated 64-char random token / 已生成 64 位随机口令"
      break
    elif (( ${#TOKEN_INPUT} < 16 )); then
      warn "token must be >= 16 chars / 口令至少 16 字符（输入了 ${#TOKEN_INPUT} 个）"
      continue
    else
      TOKEN="$TOKEN_INPUT"
      ok "using your token / 使用你提供的口令 (${#TOKEN} chars)"
      break
    fi
  done

  echo
  echo "  HTTP port = where the web UI listens. Default 8765 is usually free."
  echo "  HTTP 端口 = Web UI 监听端口。默认 8765 通常没被占用。"
  while true; do
    read -r -p "  Port / 端口 [8765]: " PORT_INPUT
    if [[ -z "$PORT_INPUT" ]]; then PORT=8765; break; fi
    if [[ "$PORT_INPUT" =~ ^[0-9]+$ ]] && (( PORT_INPUT >= 1024 && PORT_INPUT <= 65535 )); then
      PORT="$PORT_INPUT"; break
    fi
    warn "port must be 1024-65535 / 端口范围 1024-65535"
  done
  # Smart port check: detect "port held by previous muselab LaunchAgent" and
  # offer one-click cleanup instead of forcing manual kill.
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    HOLDER_PID="$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1)"
    HOLDER_NAME=""
    if [[ -n "$HOLDER_PID" ]]; then
      HOLDER_NAME="$(ps -p "$HOLDER_PID" -o comm= 2>/dev/null | xargs basename 2>/dev/null)"
    fi
    HAS_OLD_AGENT=""
    if launchctl list 2>/dev/null | grep -q com.muselab; then HAS_OLD_AGENT=1; fi

    if [[ "$HOLDER_NAME" =~ ^(python|python3|uv)$ ]] && [[ -n "$HAS_OLD_AGENT" ]]; then
      warn "Port $PORT is held by an existing muselab install (PID $HOLDER_PID, $HOLDER_NAME)"
      warn "  端口被已有的 muselab 占着 — 可以一键清理后继续"
      REPLY="$(ask 'Clean it up and continue / 清理后继续? [Y/n]:' 'Y')"
      if [[ "$REPLY" =~ ^[Yy] ]]; then
        launchctl unload "$HOME/Library/LaunchAgents/com.muselab.plist" 2>/dev/null || true
        kill -TERM "$HOLDER_PID" 2>/dev/null || true
        sleep 2
        if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
          kill -KILL "$HOLDER_PID" 2>/dev/null || true
          sleep 1
        fi
        if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
          err "Cleanup didn't free port — kill manually then re-run."
          exit 1
        fi
        ok "cleaned up — port $PORT now free"
      else
        err "Aborted by user."
        exit 1
      fi
    else
      err "Port $PORT is already in use (PID ${HOLDER_PID:-?}, ${HOLDER_NAME:-unknown})"
      err "  端口被别的进程占着，不是 muselab — 先停掉它或重跑选别的端口"
      lsof -nP -iTCP:"$PORT" -sTCP:LISTEN | head -3
      exit 1
    fi
  fi
  ok "port $PORT available / 端口 $PORT 可用"

  echo
  echo "  Archive dir = where Muse can read/write (NEVER point at \$HOME or /)"
  echo "  档案目录 = Muse 能读写的地方（不要指向 \$HOME 或 / 根目录）"
  ARCHIVE="$(ask 'Archive dir / 档案目录 (absolute path / 绝对路径):' "$HOME/muselab-archive")"
  ARCHIVE="${ARCHIVE/#\~/$HOME}"
  if ! mkdir -p "$ARCHIVE" 2>/dev/null; then
    err "cannot create $ARCHIVE (permission denied?). Pick a path under your home."
    exit 1
  fi
  if ! ( touch "$ARCHIVE/.muselab-write-test" && rm -f "$ARCHIVE/.muselab-write-test" ) 2>/dev/null; then
    err "$ARCHIVE exists but isn't writable. Run: chmod u+rwx $ARCHIVE"
    exit 1
  fi

  cat > .env <<EOF
# Generated by install-macos.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
MUSELAB_TOKEN=$TOKEN
MUSELAB_ROOT=$ARCHIVE
MUSELAB_HOST=127.0.0.1
MUSELAB_PORT=$PORT
MUSELAB_MODEL=claude-sonnet-4-6
EOF
  chmod 600 .env
  ok ".env created (mode 600)"
  ok "  MUSELAB_ROOT = $ARCHIVE"
  ok "  MUSELAB_TOKEN = ${TOKEN:0:6}…${TOKEN: -4}  (full token saved in .env)"

  # First-time setup: drop a CLAUDE.md template + subdirectory skeleton, and
  # walk the user through a short intake to populate the holistic profile.
  if [[ ! -f "$ARCHIVE/CLAUDE.md" ]]; then
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
    echo
    if [[ "$MUSE_LOCALE" == "zh" ]]; then
      echo "  Muse 是一个同时管你健康 / 职业 / 投资 / 家庭 / 生活的助手。"
      echo "  它需要先认识你（基本档案）+ 知道去哪里查你的真实材料。"
      echo "  下面是 2 分钟的入门问题，任意一题可以直接回车跳过。"
      INTAKE_PROMPT='现在生成档案目录骨架 + CLAUDE.md？ [Y/n]:'
    else
      echo "  Muse is one assistant that helps you across health / work /"
      echo "  money / people / life — simultaneously. To do that well, it"
      echo "  needs your basic profile and somewhere to find your real documents."
      echo "  This is a 2-minute intake; you can skip any question (press Enter)."
      INTAKE_PROMPT='Set up archive skeleton + CLAUDE.md now? [Y/n]:'
    fi
    REPLY="$(ask "$INTAKE_PROMPT" 'Y')"
    if [[ "$REPLY" =~ ^[Yy] ]]; then
      for sub in health work money people notes archives; do
        if [[ ! -d "$ARCHIVE/$sub" ]]; then
          mkdir -p "$ARCHIVE/$sub"
          cp "scripts/templates/archive-skeleton/$sub/$MUSE_README_SRC" \
             "$ARCHIVE/$sub/README.md"
        fi
      done
      # Drop in concrete "_example-" template files so users see the
      # shape of a typical entry. Suffix (.en.md / .zh.md) stripped on
      # destination so the user just sees _example-*.md.
      for ex in health/_example-checkup work/_example-project-log money/_example-budget; do
        src="scripts/templates/archive-skeleton/${ex}.${MUSE_LOCALE}.md"
        dest="$ARCHIVE/${ex}.md"
        if [[ -f "$src" && ! -f "$dest" ]]; then
          cp "$src" "$dest"
        fi
      done
      ok "archive skeleton created under $ARCHIVE/"

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
      # Patch with whole-line awk equality — robust against any chars in
      # the label (slashes, parens, full-width punctuation).
      _patch() {
        local label="$1" value="$2"
        [[ -z "$value" ]] && return
        awk -v lbl="$label" -v val=" $value" '
          !done && $0 == lbl { print lbl val; done=1; next } { print }
        ' "$ARCHIVE/CLAUDE.md" > "$ARCHIVE/CLAUDE.md.tmp" \
          && mv "$ARCHIVE/CLAUDE.md.tmp" "$ARCHIVE/CLAUDE.md"
      }
      if [[ "$MUSE_LOCALE" == "zh" ]]; then
        _patch "- 称呼 / 名字（你希望 Muse 叫你什么）：" "$INTAKE_NAME"
        _patch "- 出生年份（年龄段就行，不必精确）："     "$INTAKE_BIRTH"
        _patch "- 现在住在："                              "$INTAKE_CITY"
        _patch "- 我现在主要在做："                        "$INTAKE_DOING"
        _patch "- 这一年最想做成的一件事："                "$INTAKE_GOAL"
        _patch "- 当前最关心的健康问题（如有）："         "$INTAKE_HEALTH"
        STAGE_NEEDLE='（如：「大三在准备保研」'
      else
        _patch "- Name / how you'd like Muse to address you:" "$INTAKE_NAME"
        _patch "- Birth year (an age range is fine, no need for exact):" "$INTAKE_BIRTH"
        _patch "- Where you currently live:" "$INTAKE_CITY"
        _patch "- What I'm mainly doing:" "$INTAKE_DOING"
        _patch "- One thing I most want to make happen this year:" "$INTAKE_GOAL"
        _patch "- Top health concern right now (if any):" "$INTAKE_HEALTH"
        STAGE_NEEDLE='(e.g. "junior in college prepping for grad school"'
      fi
      if [[ -n "$INTAKE_STAGE" ]]; then
        STAGE_ESC="$(printf '%s' "$INTAKE_STAGE" | sed -e 's/[\\&|]/\\&/g')"
        # BSD sed needs -i ''
        sed -i '' "s|$STAGE_NEEDLE|$STAGE_ESC\\n\\n$STAGE_NEEDLE|" \
          "$ARCHIVE/CLAUDE.md"
      fi

      ok "CLAUDE.md → $ARCHIVE/CLAUDE.md (intake answers prefilled)"
      echo
      if [[ "$MUSE_LOCALE" == "zh" ]]; then
        echo "  接下来放点真实材料（按你的人生阶段选）:"
        echo "    • 健康:  体检 / 补剂 / 训练记录       → $ARCHIVE/health/"
        echo "    • 工作:  简历 / 作品集 / 学业材料     → $ARCHIVE/work/"
        echo "    • 财务:  预算 / 持仓 / 学贷 / 保单    → $ARCHIVE/money/"
        echo "    • 人:    关心的人的资料               → $ARCHIVE/people/"
        echo "    • 编辑 $ARCHIVE/CLAUDE.md 把剩下的空字段填完"
        echo "  每个子目录里都有 README.md 说明放什么。"
        echo "  下次 chat 时 Muse 会自动看到这些 — 不用重启服务。"
      else
        echo "  Next steps (what fits depends on your life stage):"
        echo "    • Health:  checkups / supplements / training logs → $ARCHIVE/health/"
        echo "    • Work:    resume / portfolio / study material    → $ARCHIVE/work/"
        echo "    • Money:   budget / holdings / loans / insurance  → $ARCHIVE/money/"
        echo "    • People:  profiles of people you care about      → $ARCHIVE/people/"
        echo "    • Open $ARCHIVE/CLAUDE.md and fill any blank fields"
        echo "  Each subdir has a README.md explaining what to put there."
        echo "  Muse picks all of this up on your next chat — no restart needed."
      fi
    fi
  fi
fi

# ----- 4. LaunchAgent ----------------------------------------------------
PORT="$(grep -E '^MUSELAB_PORT=' .env 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"
PORT="${PORT:-8765}"

bold "4/5  Installing LaunchAgent / 注册 LaunchAgent"
AGENT_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/muselab"
mkdir -p "$AGENT_DIR" "$LOG_DIR"

PLIST="$AGENT_DIR/com.muselab.plist"

# Build PATH that the agent will inherit — must include uv's dir and brew dirs so
# subprocesses (claude CLI, node for MCP) can be found by absolute resolution.
UV_DIR="$(dirname "$UV")"
PATH_DIRS="$UV_DIR:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

sed -e "s|{{REPO_PATH}}|$REPO|g" \
    -e "s|{{UV_PATH}}|$UV|g" \
    -e "s|{{PATH_DIRS}}|$PATH_DIRS|g" \
    -e "s|{{HOME_DIR}}|$HOME|g" \
    scripts/templates/com.muselab.plist.tmpl > "$PLIST"
ok "plist: $PLIST"

# Reload — unload first in case an old version is loaded
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"
# Wait up to 15s for the agent to register
WAITED=0
while (( WAITED < 15 )); do
  if launchctl list 2>/dev/null | grep -q com.muselab; then break; fi
  sleep 1; WAITED=$((WAITED+1))
done
if launchctl list 2>/dev/null | grep -q com.muselab; then
  ok "agent loaded (took ${WAITED}s)"
else
  err "agent failed to load in 15s — check $LOG_DIR/stderr.log"
  exit 1
fi

# ----- 5. Sanity check ---------------------------------------------------
bold "5/5  Sanity check / 启动自检"
# Up to 30s for HTTP to come up (first-boot SDK init)
WAITED=0
while (( WAITED < 30 )); do
  if curl -fs -o /dev/null -m 3 http://127.0.0.1:$PORT/ 2>/dev/null; then break; fi
  sleep 1; WAITED=$((WAITED+1))
done
if curl -fs -o /dev/null -m 3 http://127.0.0.1:$PORT/ 2>/dev/null; then
  ok "muselab responding at http://localhost:$PORT (took ${WAITED}s)"
else
  warn "didn't respond at http://localhost:$PORT in 30s — give it more time or tail logs:"
  warn "  tail -f $LOG_DIR/stderr.log"
fi

echo
bold "✓ muselab installed / 安装完成"
echo  "  Open / 打开:   http://localhost:$PORT"
echo
TOKEN_NOW="$(grep -E '^MUSELAB_TOKEN=' .env 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"
if [[ -n "$TOKEN_NOW" ]]; then
  echo  "  Login token / 登录口令（复制贴进浏览器登录框）:"
  printf  "    \033[1;36m%s\033[0m\n" "$TOKEN_NOW"
  echo  "  Saved at / 也存在: $REPO/.env  →  grep MUSELAB_TOKEN .env"
fi
echo
echo  "  Useful commands / 常用命令:"
echo  "    launchctl list | grep muselab               # check loaded / 查状态"
echo  "    launchctl kickstart -k gui/\$UID/com.muselab # restart / 重启"
echo  "    tail -f $LOG_DIR/stderr.log                 # tail logs / 看日志"
echo  "    bash scripts/uninstall-macos.sh             # remove autostart / 卸载"

# Auto-open the URL in the user's default browser. Skip via MUSELAB_NO_BROWSER=1.
# Token is intentionally NOT put in the URL — would end up in browser history.
# User pastes from the highlighted line above into the login form.
if [[ -z "${MUSELAB_NO_BROWSER:-}" ]] && command -v open >/dev/null 2>&1; then
  echo
  echo  "  Opening browser… (set MUSELAB_NO_BROWSER=1 to skip)"
  open "http://localhost:$PORT" 2>/dev/null &
fi
