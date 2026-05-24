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

# Non-interactive mode (CI / Docker / demo recording): export
# MUSELAB_NONINTERACTIVE=1 to take every default and skip every prompt.
NONINT="${MUSELAB_NONINTERACTIVE:-0}"
ask() {
  local q="$1" def="${2:-}" ans
  if [[ "$NONINT" == "1" ]]; then
    echo "$def"
    return
  fi
  read -rp "  $q ${def:+[$def]} " ans
  echo "${ans:-$def}"
}

bold "muselab — Linux installer"
echo  "  Repo: $REPO"
if [[ "$NONINT" == "1" ]]; then
  echo  "  Mode: non-interactive (all defaults, no prompts)"
fi
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

command -v claude >/dev/null 2>&1 && ok "claude CLI: $(command -v claude)"

# uvx ships with uv → almost always present once uv is installed
if command -v uvx >/dev/null 2>&1; then
  ok "uvx present — uv-based MCP servers (fetch, git, time, …) available"
else
  warn "uvx not found — uv-based MCP presets (fetch, git, time) won't run"
  warn "  install: comes with uv (already required); make sure uv is on PATH"
fi

# Auto-install Node LTS + claude CLI when missing. Both are user-scoped
# (fnm goes into ~/.local/share/fnm, npm -g into ~/.npm-global after the
# fnm switch) so no sudo. Skipping these used to leave the install in a
# "technically working but Muse can't do anything" state — claude 401s and
# the default memory / sequential-thinking / filesystem MCP presets all
# silent-fail. With this block, the one-line install really is end-to-end.
NEED_CLAUDE_LOGIN=0
INSTALL_NODE=0
INSTALL_CLAUDE=0
command -v node >/dev/null 2>&1   || INSTALL_NODE=1
command -v claude >/dev/null 2>&1 || INSTALL_CLAUDE=1

if (( INSTALL_NODE )) || (( INSTALL_CLAUDE )); then
  echo
  bold "Optional auto-install / 可选自动安装"
  (( INSTALL_NODE   )) && echo "  - Node LTS (via fnm — user-scoped, no sudo, ~30s)"
  (( INSTALL_CLAUDE )) && echo "  - Anthropic claude CLI (npm install -g, ~10s)"
  echo "  Why: powers the default MCP presets (memory / sequential-thinking /"
  echo "  filesystem) + lets you reuse a Claude Pro / Max subscription."
  echo "  原因：默认 MCP 预设和复用 Claude Pro/Max 订阅都需要它们。"
  REPLY="$(ask 'Install now / 现在装? [Y/n]:' 'Y')"
  if [[ "$REPLY" =~ ^[Yy] ]]; then
    if (( INSTALL_NODE )); then
      bold "Installing fnm + Node LTS…"
      # fnm installer touches ~/.bashrc / ~/.zshrc so future shells pick it up.
      curl -fsSL https://fnm.vercel.app/install | bash
      # Source for THIS shell so the npm install below works immediately.
      export PATH="$HOME/.local/share/fnm:$PATH"
      if command -v fnm >/dev/null 2>&1; then
        eval "$(fnm env --shell bash)"
        fnm install --lts
        # fnm version aliases changed across releases — try the common ones.
        fnm default lts/latest 2>/dev/null || fnm default lts-latest 2>/dev/null || true
        fnm use     lts/latest 2>/dev/null || fnm use     lts-latest 2>/dev/null || true
        eval "$(fnm env --shell bash)"
      fi
      if command -v node >/dev/null 2>&1; then
        ok "node $(node --version) · npm $(npm --version)"
      else
        warn "fnm install ran but node not on PATH. Open a new shell, then:"
        warn "  cd $REPO && bash scripts/install-linux.sh   # re-run"
      fi
    fi

    if (( INSTALL_CLAUDE )) && command -v npm >/dev/null 2>&1; then
      bold "Installing @anthropic-ai/claude-code via npm…"
      npm install -g @anthropic-ai/claude-code
      if command -v claude >/dev/null 2>&1; then
        ok "claude CLI: $(command -v claude)"
        NEED_CLAUDE_LOGIN=1
      else
        warn "npm install ran but 'claude' not on PATH yet — check 'npm root -g'"
      fi
    elif (( INSTALL_CLAUDE )); then
      warn "skipped claude CLI install — no npm (Node install failed?)"
    fi
  else
    warn "Skipped. Without Node+claude CLI: Anthropic models 401, default MCP presets disabled."
    warn "  To install later:  curl -fsSL https://fnm.vercel.app/install | bash && fnm install --lts"
    warn "                     npm install -g @anthropic-ai/claude-code && claude login"
  fi
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
    if [[ "$NONINT" == "1" ]]; then
      TOKEN_INPUT=""   # non-interactive → auto-generate
    else
      read -r -p "  Token (Enter for random / 回车随机): " TOKEN_INPUT
    fi
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
    if [[ "$NONINT" == "1" ]]; then
      PORT_INPUT=""   # non-interactive → default port
    else
      read -r -p "  Port / 端口 [8765]: " PORT_INPUT
    fi
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
    # Locale detection — Chinese template if user's shell locale is zh,
    # English template otherwise. $LANG / $LC_ALL / $LC_MESSAGES checked.
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
      # 1) Copy subdirectory skeleton (each with a README explaining what to
      #    put there). User gets clean "README.md" regardless of locale.
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
      for ex in health/_example-checkup work/_example-project-log money/_example-budget notes/_example-weekly-review people/_example-person-card; do
        src="scripts/templates/archive-skeleton/${ex}.${MUSE_LOCALE}.md"
        dest="$ARCHIVE/${ex}.md"
        if [[ -f "$src" && ! -f "$dest" ]]; then
          cp "$src" "$dest"
        fi
      done
      ok "archive skeleton created under $ARCHIVE/"

      # 2) Quick intake — just enough to make Muse's first reply useful.
      # All questions are open-ended so they fit students / employees / freelancers /
      # parents / retirees alike. Press Enter to skip any.
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

      # 3) Write CLAUDE.md with the intake values prefilled.
      sed -e "s|%DATE%|$(date +%Y-%m-%d)|" \
        "$MUSE_CLAUDE_TPL" > "$ARCHIVE/CLAUDE.md"
      # Patch the empty profile slots with whatever the user gave. Use awk
      # with whole-line string equality — robust against any chars in the
      # label (slashes, parentheses, full-width punctuation).
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
      # life stage 自由文本：插在「一句话现在」段的提示行上方。
      if [[ -n "$INTAKE_STAGE" ]]; then
        STAGE_ESC="$(printf '%s' "$INTAKE_STAGE" | sed -e 's/[\\&|]/\\&/g')"
        sed -i "s|$STAGE_NEEDLE|$STAGE_ESC\\n\\n$STAGE_NEEDLE|" \
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
echo  "  Open / 打开:   http://localhost:$PORT"
echo
# Read token back from .env (works whether we just wrote it or reused existing)
TOKEN_NOW="$(grep -E '^MUSELAB_TOKEN=' .env 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"
if [[ -n "$TOKEN_NOW" ]]; then
  echo  "  Login token / 登录口令（复制贴进浏览器登录框）:"
  # Only emit ANSI color when stdout is a TTY — otherwise the user piping
  # to a file / tee / CI log would copy the literal escape codes into the
  # browser as part of the token.
  if [[ -t 1 ]]; then
    printf  "    \033[1;36m%s\033[0m\n" "$TOKEN_NOW"
  else
    printf  "    %s\n" "$TOKEN_NOW"
  fi
  echo  "  Saved at / 也存在: $REPO/.env  →  grep MUSELAB_TOKEN .env"
fi
echo
if (( NEED_CLAUDE_LOGIN )); then
  bold "⚠  One more step / 还差一步"
  echo  "  claude CLI is installed but not logged in. Run this once to enable Claude:"
  echo  "  Anthropic 凭证还没登。跑下面这一行（一次性，浏览器 OAuth）："
  echo
  if [[ -t 1 ]]; then
    printf  "    \033[1;36m%s\033[0m\n" "claude login"
  else
    printf  "    %s\n" "claude login"
  fi
  echo
fi
echo  "  Useful commands / 常用命令:"
echo  "    systemctl --user status muselab     # check status / 查状态"
echo  "    systemctl --user restart muselab    # restart / 重启"
echo  "    journalctl --user -u muselab -f     # tail logs / 看日志"
echo  "    bash scripts/uninstall-linux.sh     # remove autostart / 卸载"

# Auto-open the URL in the user's default browser. Silent-fail when on a
# headless server (no DISPLAY / xdg-open). User can skip via MUSELAB_NO_BROWSER=1.
# Token is intentionally NOT put in the URL — would end up in browser history
# and shared with anyone who can see the screen. Paste it from the .env line above.
if [[ -z "${MUSELAB_NO_BROWSER:-}" ]] && command -v xdg-open >/dev/null 2>&1 \
    && [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
  echo
  echo  "  Opening browser… (set MUSELAB_NO_BROWSER=1 to skip)"
  xdg-open "http://localhost:$PORT" >/dev/null 2>&1 &
fi
