#!/usr/bin/env bash
# muselab — one-shot Linux installer (user-level systemd service)
# Usage: bash scripts/install-linux.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
err()  { printf "  \033[31m✗\033[0m %s\n" "$*" >&2; }
ask()  { local q="$1" def="${2:-}" ans; read -rp "  $q ${def:+[$def]} " ans; echo "${ans:-$def}"; }

bold "muselab — Linux installer"
echo  "  Repo: $REPO"
echo

# ----- 1. Prerequisites ---------------------------------------------------
bold "1/5  Checking prerequisites"

# Refuse sudo / root — service goes under your normal user
if [[ $EUID -eq 0 ]]; then
  err "Don't run this with sudo / as root."
  echo "      muselab runs as a user-level systemd service (no root needed)."
  echo "      Run as your normal user: bash scripts/install-linux.sh"
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  err "uv not found. Install it first:"
  echo "      curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "      Then re-source your shell or open a new terminal."
  exit 1
fi
UV="$(command -v uv)"
ok "uv: $UV"

# Python 3.12+ required (claude-agent-sdk + modern type syntax we use).
# uv will install one if missing, but warn the user so the slow first sync
# doesn't surprise them.
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
fi

# MCP runtimes — non-fatal but warn so user knows which presets will work.
if command -v uvx >/dev/null 2>&1; then
  ok "uvx present — uv-based MCP servers (fetch, git, time, …) available"
else
  warn "uvx not found — uv-based MCP presets (fetch, git, time) won't run"
  warn "  install: comes with uv (already required); make sure uv is on PATH"
fi
if command -v npx >/dev/null 2>&1; then
  ok "npx present — npm-based MCP servers (memory, sequential-thinking, …) available"
else
  warn "npx not found — npm-based MCP presets (memory, sequential-thinking, filesystem) won't run"
  warn "  install: https://nodejs.org or  curl -fsSL https://fnm.vercel.app/install | bash && fnm install --lts"
fi

if ! command -v systemctl >/dev/null 2>&1; then
  err "systemctl not found. This installer needs systemd (most modern distros)."
  exit 1
fi
ok "systemctl present"

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
      if command -v openssl >/dev/null 2>&1; then
        TOKEN="$(openssl rand -hex 32)"
      else
        TOKEN="$(head -c 32 /dev/urandom | xxd -p -c 999)"
      fi
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
  # Check port free; if held by an existing muselab systemd service, offer
  # one-click cleanup instead of making the user run commands by hand.
  if command -v ss >/dev/null 2>&1 && ss -tlnH "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then
    HOLDER_PID="$(ss -tlnpH "sport = :$PORT" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1)"
    HOLDER_NAME=""
    if [[ -n "$HOLDER_PID" ]]; then
      HOLDER_NAME="$(ps -p "$HOLDER_PID" -o comm= 2>/dev/null | tr -d ' ')"
    fi
    HAS_OLD_UNIT=""
    if systemctl --user is-enabled muselab.service >/dev/null 2>&1; then HAS_OLD_UNIT=1; fi

    if [[ "$HOLDER_NAME" =~ ^(python|uv)$ ]] && [[ -n "$HAS_OLD_UNIT" ]]; then
      warn "Port $PORT is held by an existing muselab install (PID $HOLDER_PID, $HOLDER_NAME)"
      warn "  端口被已有的 muselab 占着 — 可以一键清理后继续"
      REPLY="$(ask 'Clean it up and continue / 清理后继续? [Y/n]:' 'Y')"
      if [[ "$REPLY" =~ ^[Yy] ]]; then
        systemctl --user stop muselab.service 2>/dev/null || true
        systemctl --user disable muselab.service 2>/dev/null || true
        sleep 2
        if ss -tlnH "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then
          err "Cleanup didn't free port — process may not be ours. Kill manually then re-run."
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
      ss -tlnp "sport = :$PORT" 2>&1 | head -3
      exit 1
    fi
  fi
  ok "port $PORT available / 端口 $PORT 可用"

  echo
  echo "  Archive dir = where Muse can read/write (NEVER point at \$HOME or /)"
  ARCHIVE="$(ask 'Archive dir (absolute path):' "$HOME/muselab-archive")"
  ARCHIVE="${ARCHIVE/#\~/$HOME}"
  if ! mkdir -p "$ARCHIVE" 2>/dev/null; then
    err "cannot create $ARCHIVE (permission denied?). Pick a path under your home."
    exit 1
  fi
  # Probe write — fails fast if dir is read-only / on full disk / odd permissions
  if ! ( touch "$ARCHIVE/.muselab-write-test" && rm -f "$ARCHIVE/.muselab-write-test" ) 2>/dev/null; then
    err "$ARCHIVE exists but isn't writable. Run: chmod u+rwx $ARCHIVE"
    exit 1
  fi

  cat > .env <<EOF
# Generated by install-linux.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
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
    echo
    echo "  Muse is one assistant that helps you across health / career / "
    echo "  investment / family / life — simultaneously. To do that well, it"
    echo "  needs your basic profile and somewhere to find your real documents."
    echo "  This is a 2-minute intake; you can skip any question (press Enter)."
    echo
    echo "  Muse 是一个同时管你健康 / 职业 / 投资 / 家庭 / 生活的助手。"
    echo "  它需要先认识你（基本档案）+ 知道去哪里查你的真实材料。"
    echo "  下面是 2 分钟的入门问题，任意一题可以直接回车跳过。"
    REPLY="$(ask 'Set up archive skeleton + CLAUDE.md now / 现在生成档案目录骨架 + CLAUDE.md？ [Y/n]:' 'Y')"
    if [[ "$REPLY" =~ ^[Yy] ]]; then
      # 1) Copy subdirectory skeleton (health/ career/ investment/ family/
      #    notes/ archives/, each with a README explaining what to put there).
      for sub in health work money people notes archives; do
        if [[ ! -d "$ARCHIVE/$sub" ]]; then
          mkdir -p "$ARCHIVE/$sub"
          cp "scripts/templates/archive-skeleton/$sub/README.md" \
             "$ARCHIVE/$sub/README.md"
        fi
      done
      ok "archive skeleton created under $ARCHIVE/"

      # 2) Quick intake — just enough to make Muse's first reply useful.
      # All questions are open-ended so they fit students / employees / freelancers /
      # parents / retirees alike. Press Enter to skip any.
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

      # 3) Write CLAUDE.md with the intake values prefilled.
      sed -e "s|%DATE%|$(date +%Y-%m-%d)|" \
        scripts/templates/default-CLAUDE.md > "$ARCHIVE/CLAUDE.md"
      # Patch the empty profile slots with whatever the user gave. Each
      # substitution targets the END of the matching "- 标签：" line.
      _patch() {
        local label="$1" value="$2"
        [[ -z "$value" ]] && return
        # escape | for sed and append after the colon
        local esc
        esc="$(printf '%s' "$value" | sed -e 's/[\\&|]/\\&/g')"
        sed -i -E "0,/^(- $label：)$/{s||\1 $esc|}" "$ARCHIVE/CLAUDE.md"
      }
      _patch "称呼 / 名字（你希望 Muse 叫你什么）" "$INTAKE_NAME"
      _patch "出生年份（年龄段就行，不必精确）"     "$INTAKE_BIRTH"
      _patch "现在住在"                              "$INTAKE_CITY"
      _patch "我现在主要在做"                        "$INTAKE_DOING"
      _patch "这一年最想做成的一件事"               "$INTAKE_GOAL"
      _patch "当前最关心的健康问题（如有）"         "$INTAKE_HEALTH"
      # life stage 自由文本，放在「一句话现在」上方的备注里。
      # `local` 只能在函数内用，所以这里裸用变量。
      if [[ -n "$INTAKE_STAGE" ]]; then
        STAGE_ESC="$(printf '%s' "$INTAKE_STAGE" | sed -e 's/[\\&|]/\\&/g')"
        sed -i "s|（如：「大三在准备保研」|$STAGE_ESC\\n\\n（如：「大三在准备保研」|" \
          "$ARCHIVE/CLAUDE.md"
      fi

      ok "CLAUDE.md → $ARCHIVE/CLAUDE.md (with your intake answers prefilled / 你填的字段已预填)"
      echo
      echo "  Next steps / 接下来放点真实材料 (what fits depends on your life stage):"
      echo "    • Health / 健康:  checkups / supplements / training logs → $ARCHIVE/health/"
      echo "                      体检 / 补剂 / 训练记录"
      echo "    • Work   / 工作:  resume / portfolio / study material    → $ARCHIVE/work/"
      echo "                      简历 / 作品集 / 学业材料"
      echo "    • Money  / 财务:  budget / holdings / loans / insurance  → $ARCHIVE/money/"
      echo "                      预算 / 持仓 / 学贷 / 保单"
      echo "    • People / 人:    profiles of people you care about      → $ARCHIVE/people/"
      echo "                      你关心的人的资料"
      echo "    • Open / 编辑 $ARCHIVE/CLAUDE.md  to fill in any blank fields / 把剩下的空字段填完"
      echo "  Each subdir has a README.md / 每个子目录里都有 README.md 说明放什么。"
      echo "  Muse picks all of this up on your next chat — no restart needed."
      echo "  下次 chat 时 Muse 会自动看到这些 — 不用重启服务。"
    fi
  fi
fi

# Reload PORT from .env (may be non-default if user picked one, or from a
# pre-existing .env in the "keeping as is" branch).
PORT="$(grep -E '^MUSELAB_PORT=' .env 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"
PORT="${PORT:-8765}"

# ----- 4. systemd user service -------------------------------------------
bold "4/5  Installing systemd --user service / 注册 systemd 用户服务"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
sed -e "s|{{REPO_PATH}}|$REPO|g" \
    -e "s|{{UV_PATH}}|$UV|g" \
    scripts/templates/muselab.service.tmpl > "$UNIT_DIR/muselab.service"
ok "unit file: $UNIT_DIR/muselab.service"

systemctl --user daemon-reload
systemctl --user enable --now muselab.service
# Wait up to 30s for the service to become active — first-boot SDK initialisation
# on slow VPS can take 5-10s, and a flat sleep 1 was reporting false failures.
WAITED=0
while (( WAITED < 30 )); do
  if systemctl --user is-active --quiet muselab.service; then break; fi
  sleep 1; WAITED=$((WAITED+1))
done
if systemctl --user is-active --quiet muselab.service; then
  ok "service is active (took ${WAITED}s)"
else
  err "service didn't become active in 30s — check logs: journalctl --user -u muselab -n 80"
  exit 1
fi

# ----- 5. Linger (so service runs even when you're not logged in) --------
bold "5/5  Enable user lingering / 启用用户级常驻 (so service survives logout / reboot)"
if loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
  ok "linger already enabled for $USER"
else
  warn "linger NOT enabled — service will stop when you log out / reboot without login"
  warn "  To enable (requires sudo):"
  warn "    sudo loginctl enable-linger $USER"
fi

echo
bold "✓ muselab installed / 安装完成"
echo  "  Open  / 打开    → http://localhost:$PORT"
echo  "  Token / 登录口令 → grep MUSELAB_TOKEN .env"
echo
echo  "  Useful commands / 常用命令:"
echo  "    systemctl --user status muselab     # check status / 查状态"
echo  "    systemctl --user restart muselab    # restart / 重启"
echo  "    journalctl --user -u muselab -f     # tail logs / 看日志"
echo  "    bash scripts/uninstall-linux.sh     # remove autostart / 卸载"
