#!/usr/bin/env bash
# muselab — one-shot macOS installer (user-level LaunchAgent)
# Usage: bash scripts/install-macos.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# Pinned external tool versions (claude CLI etc.) — single source shared
# with install-linux.sh. Keep in lockstep with Dockerfile.
# shellcheck source=scripts/versions.env
. "$REPO/scripts/versions.env"

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

# When invoked via `curl ... | bash` (the one-line install), our stdin is
# the pipe — and every interactive `read` would immediately EOF, causing
# `set -e` to silently abort the script after just printing the prompt.
# Detect that case and reattach stdin to the controlling terminal. The
# `[[ ! -t 0 ]]` guard makes this a no-op when the script is run directly
# (e.g. `bash scripts/install-macos.sh`), so no behavior change there.
if [[ "$NONINT" != "1" ]] && [[ ! -t 0 ]] && [[ -c /dev/tty ]]; then
  exec </dev/tty
fi

# Locale for files written to disk (CLAUDE.md template + archive READMEs).
# Defaults to zh if the shell locale is Chinese, en otherwise. Override
# explicitly with MUSELAB_LOCALE=zh|en. The installer's prompts/output
# themselves are always bilingual.
MUSE_LOCALE="${MUSELAB_LOCALE:-}"
if [[ -z "$MUSE_LOCALE" ]]; then
  if [[ "${LANG:-}${LC_ALL:-}${LC_MESSAGES:-}" == *zh* ]]; then
    MUSE_LOCALE=zh
  else
    MUSE_LOCALE=en
  fi
fi

bold "muselab — macOS installer"
echo  "  Repo: $REPO"
echo  "  Archive content language / 档案内容语言: $MUSE_LOCALE  (override: MUSELAB_LOCALE=zh|en)"
if [[ "$NONINT" == "1" ]]; then
  echo  "  Mode: non-interactive (all defaults, no prompts)"
fi
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

# uv discovery — common pitfall on macOS: user installed uv via Homebrew or
# the official installer, but launched this script from a shell whose PATH
# doesn't include the install dir (e.g. default-shell zsh has brew shellenv
# wired up in ~/.zshrc, but the user dropped into `bash` which reads
# ~/.bash_profile and misses it). Before failing, transparently retry with
# the standard install dirs appended to PATH.
if ! command -v uv >/dev/null 2>&1; then
  EXTRA_PATHS=(
    "/opt/homebrew/bin"           # Apple Silicon Homebrew
    "/usr/local/bin"              # Intel Homebrew
    "$HOME/.local/bin"            # astral.sh/uv/install.sh default
    "$HOME/.cargo/bin"            # legacy cargo install path
  )
  for p in "${EXTRA_PATHS[@]}"; do
    if [[ -x "$p/uv" ]]; then
      export PATH="$p:$PATH"
      warn "uv found at $p/uv but wasn't on PATH — added it for this run"
      warn "  (permanent fix: add 'export PATH=\"$p:\$PATH\"' to ~/.bash_profile or ~/.zshrc)"
      break
    fi
  done
fi
if ! command -v uv >/dev/null 2>&1; then
  err "uv not found. Install it first:"
  echo "      curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "      # or:  brew install uv"
  echo "      Then open a new shell so PATH picks it up, and re-run this script."
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

if command -v claude >/dev/null 2>&1; then
  ok "claude CLI: $(command -v claude)"
  if [[ ! -d "$HOME/.claude" ]]; then
    warn "  ~/.claude not found — run 'claude login' before installing for OAuth to work"
  fi
fi

# uvx ships with uv → almost always present once uv is installed
if command -v uvx >/dev/null 2>&1; then
  ok "uvx present — uv-based MCP servers (fetch, git, time, …) available"
else
  warn "uvx not found — uv-based MCP presets (fetch, git, time) won't run"
fi

# Auto-install Node LTS + claude CLI when missing. macOS prefers Homebrew
# if present (most users have it for uv anyway); falls back to fnm so the
# install never blocks on "you need to brew install X first".
# Skipping these used to leave Muse in a "running but Claude 401s + default
# MCP presets silent-fail" state — with this block the one-line install is
# really end-to-end.
NEED_CLAUDE_LOGIN=0
INSTALL_NODE=0
INSTALL_CLAUDE=0
command -v node >/dev/null 2>&1   || INSTALL_NODE=1
command -v claude >/dev/null 2>&1 || INSTALL_CLAUDE=1

if (( INSTALL_NODE )) || (( INSTALL_CLAUDE )); then
  echo
  bold "Optional auto-install / 可选自动安装"
  (( INSTALL_NODE   )) && echo "  - Node LTS (brew if present, else fnm — both user-scoped, no sudo)"
  (( INSTALL_CLAUDE )) && echo "  - Anthropic claude CLI (npm install -g, ~10s)"
  echo "  Why: powers the default MCP presets (memory / sequential-thinking /"
  echo "  filesystem) + lets you reuse a Claude Pro / Max subscription."
  echo "  原因：默认 MCP 预设和复用 Claude Pro/Max 订阅都需要它们。"
  REPLY="$(ask 'Install now / 现在装? [Y/n]:' 'Y')"
  if [[ "$REPLY" =~ ^[Yy] ]]; then
    if (( INSTALL_NODE )); then
      if command -v brew >/dev/null 2>&1; then
        bold "Installing Node LTS via Homebrew…"
        brew install node
      else
        bold "Installing fnm + Node LTS (no brew detected)…"
        # Supply-chain note: pipes fnm's official upstream installer to bash
        # with no hash check (their documented method). Trusted over HTTPS;
        # full pinning is out of scope.
        curl -fsSL https://fnm.vercel.app/install | bash
        export PATH="$HOME/Library/Application Support/fnm:$HOME/.local/share/fnm:$PATH"
        if command -v fnm >/dev/null 2>&1; then
          eval "$(fnm env --shell bash)"
          fnm install --lts
          fnm default lts/latest 2>/dev/null || fnm default lts-latest 2>/dev/null || true
          fnm use     lts/latest 2>/dev/null || fnm use     lts-latest 2>/dev/null || true
          eval "$(fnm env --shell bash)"
        fi
      fi
      if command -v node >/dev/null 2>&1; then
        ok "node $(node --version) · npm $(npm --version)"
      else
        warn "Node install ran but binary not on PATH. Open a new shell and re-run installer."
      fi
    fi

    if (( INSTALL_CLAUDE )) && command -v npm >/dev/null 2>&1; then
      bold "Installing @anthropic-ai/claude-code@${CLAUDE_CLI_VERSION} via npm…"
      npm install -g "@anthropic-ai/claude-code@${CLAUDE_CLI_VERSION}"
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
    warn "  To install later:  brew install node  (or fnm install --lts)"
    warn "                     npm install -g @anthropic-ai/claude-code && claude login"
  fi
fi

# ----- 2. Python deps ----------------------------------------------------
bold "2/5  Installing Python dependencies / 安装 Python 依赖 (uv sync, may take a few minutes first time)"
uv sync --frozen                     # --frozen: install exactly uv.lock (matches Docker); no implicit re-resolve
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

    # Holder-name match is best-effort: macOS reports framework Python as
    # "Python"/"python3.12" etc., so match case-insensitively by prefix.
    # But the decisive signal is a loaded com.muselab agent — if that's
    # present the port holder IS our stale instance regardless of comm name.
    HOLDER_IS_PY=""
    shopt -s nocasematch
    [[ "$HOLDER_NAME" =~ ^(python|uv) ]] && HOLDER_IS_PY=1
    shopt -u nocasematch

    if [[ -n "$HAS_OLD_AGENT" ]] && { [[ -n "$HOLDER_IS_PY" ]] || [[ -n "$HOLDER_PID" ]]; }; then
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
# MUSELAB_MODEL intentionally NOT set here — the frontend picks the first
# configured provider on first load. Set this only if you have a strong
# preference AND have configured the matching provider (e.g.
# MUSELAB_MODEL=deepseek-v4-pro after setting DEEPSEEK_API_KEY).
EOF
  chmod 600 .env
  ok ".env created (mode 600)"
  ok "  MUSELAB_ROOT = $ARCHIVE"
  ok "  MUSELAB_TOKEN = ${TOKEN:0:6}…${TOKEN: -4}  (full token saved in .env)"

  # First-time setup: drop a CLAUDE.md template + subdirectory skeleton, and
  # walk the user through a short intake to populate the holistic profile.
  if [[ ! -f "$ARCHIVE/CLAUDE.md" ]]; then
    # MUSE_LOCALE was decided at the top of the script. Map it to template paths.
    if [[ "$MUSE_LOCALE" == "zh" ]]; then
      MUSE_CLAUDE_TPL="scripts/templates/default-CLAUDE.md"
      MUSE_README_SRC="README.md"
    else
      MUSE_CLAUDE_TPL="scripts/templates/default-CLAUDE.en.md"
      MUSE_README_SRC="README.en.md"
    fi
    echo
    echo "  Muse is one assistant — health / work / money / people / life all at once."
    echo "  Muse 是一个同时覆盖健康、工作、财务、人际关系与生活的助手。"
    echo "  Below is a 2-minute intake; press Enter to skip any question."
    echo "  下面 2 分钟入门问答，任意一题直接回车即跳过。"
    REPLY="$(ask 'Set up archive skeleton + CLAUDE.md / 现在生成档案目录骨架 + CLAUDE.md? [Y/n]:' 'Y')"
    if [[ "$REPLY" =~ ^[Yy] ]]; then
      for sub in health work money people notes archives; do
        if [[ ! -d "$ARCHIVE/$sub" ]]; then
          mkdir -p "$ARCHIVE/$sub"
          cp "scripts/templates/archive-skeleton/$sub/$MUSE_README_SRC" \
             "$ARCHIVE/$sub/README.md"
        fi
      done
      ok "archive skeleton created under $ARCHIVE/"

      echo
      echo "  --- Quick intake / 入门问答 (Enter to skip / 回车跳过) ---"
      INTAKE_NAME="$(ask 'How should Muse address you? / Muse 该怎么称呼你？' '')"
      INTAKE_BIRTH="$(ask 'Birth year or age range / 出生年份（或大致年龄段）:' '')"
      INTAKE_CITY="$(ask 'Where do you live? / 你现在住在哪？' '')"
      echo "  What occupies most of your week? / 这一周你的时间主要用在哪里？"
      echo "    (study / job / freelance / care / retirement / … —— 学业 / 工作 / 自由职业 / 照护 / 退休 / 其他)"
      INTAKE_DOING="$(ask '' '')"
      echo "  One sentence about your life stage right now / 用一句话描述你当下的人生阶段"
      INTAKE_STAGE="$(ask '' '')"
      INTAKE_GOAL="$(ask 'One main goal for this year / 这一年最想做成的一件事:' '')"
      INTAKE_HEALTH="$(ask 'Top health concern (or "none") / 当前最关心的健康问题（无则填 none）:' '')"

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

      ok "CLAUDE.md → $ARCHIVE/CLAUDE.md (intake answers prefilled / 入门答案已写入)"
      echo
      echo "  Next steps — drop real files into these directories:"
      echo "  接下来：把你的实际文件放进对应目录:"
      echo "    • Health 健康:  checkups / supplements / training       → $ARCHIVE/health/"
      echo "    • Work 工作:    resume / portfolio / study material      → $ARCHIVE/work/"
      echo "    • Money 财务:   budget / holdings / loans / insurance    → $ARCHIVE/money/"
      echo "    • People 人:    profiles of people you care about        → $ARCHIVE/people/"
      echo "  Edit $ARCHIVE/CLAUDE.md to fill any remaining blank fields."
      echo "  编辑 $ARCHIVE/CLAUDE.md 把剩下的空字段填完。"
      echo "  Each subdir has a README.md explaining what to put there."
      echo "  每个子目录里都有 README.md 说明放什么。"
      echo "  Muse picks all of this up on your next chat — no restart needed."
      echo "  下次对话时 Muse 自动看到这些 —— 不用重启服务。"
    fi
  fi
fi

# ----- 4. LaunchAgent ----------------------------------------------------
PORT="$(grep -E '^MUSELAB_PORT=' .env 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"
PORT="${PORT:-8765}"

# MUSELAB_SKIP_SERVICE=1 short-circuits steps 4+5. CI-only escape hatch
# (GHA macOS runners can technically register LaunchAgents, but skipping
# keeps the test focused on the installer logic itself). End users should
# never set this.
if [[ "${MUSELAB_SKIP_SERVICE:-0}" == "1" ]]; then
  LOG_DIR="$HOME/Library/Logs/muselab"   # still referenced in final hints
  warn "4/5+5/5 SKIPPED (MUSELAB_SKIP_SERVICE=1) — no LaunchAgent registered"
  warn "  To run muselab manually: uv run python -m backend.main"
else
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
fi

echo
bold "✓ muselab installed / 安装完成"
echo  "  Open / 打开:   http://localhost:$PORT"
echo
TOKEN_NOW="$(grep -E '^MUSELAB_TOKEN=' .env 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"
if [[ -n "$TOKEN_NOW" ]]; then
  echo  "  Login token / 登录口令（复制并粘贴到浏览器登录框）:"
  # Only emit ANSI color when stdout is a TTY — otherwise piping to a file
  # / tee / CI log would copy the literal escape codes into the browser
  # alongside the token.
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
  echo  "  Anthropic 凭证尚未登录。运行下面这行命令（一次性，浏览器 OAuth）："
  echo
  if [[ -t 1 ]]; then
    printf  "    \033[1;36m%s\033[0m\n" "claude login"
  else
    printf  "    %s\n" "claude login"
  fi
  echo
fi
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
