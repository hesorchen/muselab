# 基础设施

> [English](infrastructure.md)

本页梳理 muselab 的每一个操作层——安装脚本、服务单元、Docker 镜像、开发服务器、测试套件与 CI/CD 流水线。面向需要理解**现有内容及其整体关系**的贡献者和运维人员。分步操作指南请参阅 [快速入门](quickstart_zh.md)、[Linux 安装](install-linux_zh.md)、[macOS 安装](install-macos_zh.md)、[升级](upgrade_zh.md) 与 [CONTRIBUTING.md](../CONTRIBUTING.md)。

---

## 1. scripts/ 工具箱

所有自动化脚本都在 `scripts/` 目录下，每个脚本均为独立的 bash 脚本，从仓库根目录运行。

| 脚本 | 用途 | 关键环境变量 |
|------|------|------------|
| `versions.env` | 固定外部工具版本的唯一真相源。由两个平台安装脚本引用；Dockerfile 中镜像同一版本号，需手动保持同步。| — |
| `quick-install.sh` | 一行引导脚本（`curl … \| bash`）。拒绝 root 执行，检测 OS，若缺少 `uv` 则安装，提示克隆目标目录，然后通过 `exec bash` 并重新挂载 `/dev/tty` 移交给平台安装脚本，保证管道环境下交互式提示正常工作。| `MUSELAB_NONINTERACTIVE=1` |
| `install-linux.sh` | 完整的 Linux/WSL2 安装脚本。五个阶段：前置检查 → `uv sync --frozen` → 写入 `.env`（随机 token、端口、归档目录）→ 注册 systemd 用户单元 → 检查/提示 linger。包含 7 个问题的档案配置向导，负责写入 `CLAUDE.md` 与归档子目录骨架。| `MUSELAB_NONINTERACTIVE=1`、`MUSELAB_LOCALE=zh\|en`、`MUSELAB_SKIP_SERVICE=1`、`MUSELAB_NO_BROWSER=1` |
| `install-macos.sh` | 结构上与 Linux 安装脚本相同，但注册的是 launchd LaunchAgent 而非 systemd 单元。端口冲突检测使用 `lsof` 而非 `ss`；Node 安装优先用 `brew`，失败则回退到 `fnm`。| 同上四个变量 |
| `uninstall-linux.sh` | 停止并移除 systemd 单元；保留 `.env`、`sessions/` 与归档目录。| — |
| `uninstall-macos.sh` | 卸载并移除 LaunchAgent plist；数据保留策略与 Linux 版本相同。| — |
| `upgrade.sh` | 升级 `claude-agent-sdk`（`uv lock --upgrade-package`）和 `claude` CLI（`npm install -g … @latest`），以 `pytest` 做冒烟测试，失败则中止并打印回滚提示。不自动提交或重启服务，详见 [升级](upgrade_zh.md)。| — |
| `doctor.sh` | 诊断脚本（`set -uo pipefail`，不用 `-e`，以便在部分失败时继续运行）。六项检查：前置依赖 → `.env`/配置 → Python 依赖（`uv sync --frozen`）→ 服务状态 → HTTP + 鉴权探测 → provider API 密钥。阻塞性失败退出码为 1，仅有警告则退出码为 0。| — |
| `setup-https.sh` | 仅 Linux。在已有安装前增加 Caddy 反向代理，配置 SSE 安全的 `flush_interval -1`、HSTS 与 `ufw` 规则。| — |
| `intake.sh` | 独立运行 7 问题 `CLAUDE.md` 档案配置向导。覆写前会备份已有的 `CLAUDE.md`。| — |
| `lint.sh` | 对已追踪文件执行静态检查与隐私泄露检测。| `MUSELAB_LEAK_BLACKLIST` |

---

## 2. 服务管理

### Linux —— systemd 用户单元

单元文件：`~/.config/systemd/user/muselab.service`（由 `scripts/templates/muselab.service.tmpl` 生成）。

资源上限：`MemoryHigh=2G`、`MemoryMax=4G`、`LimitNOFILE=8192`、`TasksMax=4096`。重启策略：`on-failure`、`RestartSec=10`，5 分钟内最多重启 5 次。

```bash
systemctl --user restart muselab
systemctl --user reset-failed          # 5 次重启后清除崩溃计数器
journalctl --user -u muselab -f
sudo loginctl enable-linger $USER      # VPS 上保证注销/重启后持续运行
```

**VPS 注意事项：** 若未启用 `loginctl enable-linger`，用户单元会在 SSH 会话结束时停止。安装脚本会在 linger 尚未激活时发出警告。

### macOS —— launchd LaunchAgent

Plist 文件：`~/Library/LaunchAgents/com.muselab.plist`（由 `scripts/templates/com.muselab.plist.tmpl` 生成；label 为 `com.muselab`）。崩溃或非零退出时 `KeepAlive`；`ThrottleInterval=10s`；`HardResourceLimits`：8192 fd，4096 进程。日志：`~/Library/Logs/muselab/stdout.log` 与 `stderr.log`。

```bash
launchctl kickstart -k gui/$UID/com.muselab    # 重启
tail -f ~/Library/Logs/muselab/stderr.log
```

> macOS 内存限制是建议性的（jetsam，非 cgroup）。4 GiB `MemoryMax` 强制终止仅在 Linux 上生效。

---

## 3. Docker

### 两阶段构建

`Dockerfile` 使用两阶段构建以保持最终镜像体积精简：

**阶段 1 —— 构建器：** 基础镜像 `python:3.12-slim`；复制固定版本的 `uv`/`uvx`；通过 `uv sync --frozen --no-dev --no-install-project` 仅安装生产 Python 依赖。

**阶段 2 —— 运行时：** 全新 `python:3.12-slim`；安装 `curl`、`git`、Node 20 与固定版本的 Claude Code CLI；从构建器复制预构建的 `.venv`，并复制 `backend/`、`frontend/`、`skills/` 与 `scripts/templates/`；创建非 root 用户 `muse`；暴露端口 8765；声明针对 `/api/health` 的 `HEALTHCHECK`。

### docker-compose.yml

`docker-compose.yml` 以如下默认值运行单个服务：

| Compose 配置 | 默认值 | 覆盖方式 |
|-------------|--------|---------|
| 端口绑定 | `127.0.0.1:8765:8765` | `MUSELAB_BIND`、`MUSELAB_PORT` |
| 归档卷 | `./data:/data` | `ARCHIVE_DIR` |
| Claude 凭据 | `~/.claude:/home/muse/.claude` | `CLAUDE_HOME` |
| 会话卷 | `./sessions:/app/sessions` | — |
| 内存限制 | 硬限 `4g` / 预留 `1g` | — |
| `pids_limit` | `4096` | — |
| 重启策略 | `unless-stopped` | — |

`~/.claude` 挂载为读写模式（设计如此）：Claude CLI 需要写权限来刷新 OAuth token 并持久化会话历史。compose 文件强制设置 `ANTHROPIC_API_KEY=""` 与 `ANTHROPIC_AUTH_TOKEN=""`，确保 SDK 使用 OAuth 而非 console API key。

### GHCR 多架构镜像

镜像：`ghcr.io/hesorchen/muselab`

| Tag 规则 | 发布时机 |
|---------|---------|
| `latest` | 每次推送到 `main` |
| `{version}`、`{major}.{minor}`、`{major}` | Git tag `v*.*.*` |
| `sha-{short}` | 每次推送到 `main` |

架构：通过 QEMU 支持 `linux/amd64` 与 `linux/arm64`。

---

## 4. 开发模式

```bash
# 一次性配置
git clone https://github.com/hesorchen/muselab && cd muselab
uv sync
cp .env.example .env    # 填写 MUSELAB_TOKEN 和 MUSELAB_ROOT

# 启动开发服务器（热重载，无构建步骤）
make run
# 等价于：uv run uvicorn backend.main:app --host 0.0.0.0 --port 8765 --reload
```

前端是纯 HTML + Alpine.js v3（已 vendor）。没有独立的前端开发服务器，也无需 `npm install`——编辑 `frontend/*.html|js|css` 后强刷浏览器即可。完整贡献者工作流见 [CONTRIBUTING.md](../CONTRIBUTING.md)。

### Makefile 目标

| 目标 | 命令 | 说明 |
|------|------|------|
| `make run` | `uv run uvicorn … --reload` | 带热重载的开发服务器 |
| `make test` | `uv run pytest -v` | 全量测试，详细输出 |
| `make test-fast` | `uv run pytest -x --tb=short` | 首个失败即停止 |
| `make lint` | `uv run python -m compileall -q backend tests` | 仅语法检查；CI 使用 `ruff check` |

---

## 5. 测试套件

**框架：** pytest ≥ 9.1.1，附 pytest-asyncio ≥ 1.4.0。

**目录结构：** `tests/` 包含单元测试、集成测试与 `tests/e2e/` 下的 Playwright 浏览器回归。测试文件数和代码行数会持续变化，不作为稳定项目指标。E2E 由 `RUN_E2E=1` 环境变量门控。

### 隔离策略

共享 `app_module` fixture 的作用：
- monkeypatch `MUSELAB_TOKEN`、`MUSELAB_ROOT`、`MUSELAB_PORT=9999`
- 将 `MUSELAB_ENV_PATH` 重定向到临时文件，保证测试永远不会触及真实的 `.env`
- 清除所有 provider API key 环境变量
- 删除 `sys.modules` 中所有 `backend.*` 条目以强制完整重导入
- 将 `sessions/` 隔离到临时目录

`temp_root` fixture 创建一个临时归档树，包含 `notes/` 子目录、一个 `.secret` 文件和一个 `.env` 文件，专门用于路径穿越安全测试。

### 主要测试文件

| 文件 | 覆盖内容 |
|------|---------|
| `test_chat_stream.py` | SSE 流式传输、工具调用事件、取消操作 |
| `test_regressions.py` | 跨子系统 bug 回归套件 |
| `test_scheduler.py` | 定时任务运行器 |
| `test_files.py` | 文件浏览器、上传、路径穿越安全 |
| `test_sessions.py` | 会话 CRUD 与索引 |
| `test_security.py` | 鉴权绕过、token 验证 |

### E2E（Playwright）

`tests/e2e/` 使用 Playwright + Chromium，不包含在默认的 `pytest tests/` 中。需设置 `RUN_E2E=1` 并单独安装 Chromium。当前覆盖多标签页生命周期、文件预览、聊天渲染性能与移动端终端交互。

---

## 6. CI/CD

### ci.yml

触发条件：推送到 `main`、版本 tag `v*.*.*`、向 `main` 发起 PR。

| Job | Runner | 是否阻塞 | 内容 |
|-----|--------|---------|------|
| `test` | ubuntu-latest（py 3.12 + 3.13）、macos-latest（py 3.12）| 是 | `uv sync --frozen` → `pytest tests/ -v`；Linux py 3.12 上生成覆盖率报告（非阻塞，临时 `pytest-cov`）|
| `lint` | ubuntu-latest | 是 | `ruff check backend/ tests/` + `bash scripts/lint.sh` |
| `frontend-lint` | ubuntu-latest（Node 20）| 是 | `node --check` 检查 `app.js`、`sw.js`、`constants.js`、`i18n/index.js`；JSON 验证 `manifest.webmanifest` |
| `security` | ubuntu-latest | 否 | 对冻结锁文件执行 `pip-audit` |
| `e2e` | ubuntu-latest | 否 | Playwright/Chromium，通过 `pytest-rerunfailures` 重试 2 次 |
| `docker` | ubuntu-latest | 是（push job）| PR：单架构构建，不推送。main/tag：多架构构建并推送至 `ghcr.io` |

CI 测试环境变量：`MUSELAB_TOKEN=ci-test-token-1234567890abcdef-min-32`、`MUSELAB_ROOT=${{ github.workspace }}/.ci-archive`。

### install-test.yml

路径过滤覆盖安装脚本、`pyproject.toml`、`uv.lock`、`Dockerfile`、`docker-compose.yml`。在四个 OS 镜像上端到端运行真实安装程序：

| Job | Runner | 说明 |
|-----|--------|------|
| `linux` | ubuntu-22.04、ubuntu-24.04 | `MUSELAB_NONINTERACTIVE=1 MUSELAB_SKIP_SERVICE=1 MUSELAB_NO_BROWSER=1`；轮询 `/api/health` 30 秒 |
| `macos` | macos-13（Intel，`continue-on-error`）、macos-14（ARM，必须通过）| 任务超时 20 分钟 |
| `docker-run` | ubuntu-latest | 本地构建，运行容器，轮询 `/api/health`，确认 Docker `HEALTHCHECK` 在 90 秒内达到 `healthy` |

故障产物上传时明确排除 `.env`，防止 `MUSELAB_TOKEN` 泄露。

### Release

推送匹配 `v*.*.*` 的 git tag。`ci.yml` 中的 `docker` job 会自动将带完整语义化版本 tag 矩阵的多架构镜像发布到 `ghcr.io/hesorchen/muselab`。Changelog 和 GitHub Release 由维护者手动处理。

### Dependabot

每周检查 uv 依赖（分组：`claude-agent-sdk`/`anthropic*` 一个 PR；`fastapi`/`uvicorn`/`starlette`/`pydantic` 一个 PR；最多 5 个开放 PR）。每月检查 GitHub Actions 依赖。

---

## 7. 打包

**文件：** `pyproject.toml`——`requires-python = ">=3.12"`，MIT 协议。

### 关键依赖决策

| 包 | 约束 | 原因 |
|----|------|------|
| `claude-agent-sdk` | `>=0.2.120,<0.3` | 上限是刻意设置的：muselab 依赖 SDK 工具拒绝列表与 JSONL 转录格式的特定假设，跨次版本升级必须显式验证。|
| `starlette` | `>=1.3.1` | 显式使用已修复安全问题的版本。|
| `pyjwt[crypto]` | `>=2.13.0` | 固定在 mcp 传递依赖 2.12.1 之上（PYSEC-2026-175/177/178/179）。|

### uv 用法

| 命令 | 使用场景 |
|------|---------|
| `uv sync --frozen` | 所有安装脚本、CI、Docker 构建——确保从 `uv.lock` 精确复现 |
| `uv lock --upgrade-package claude-agent-sdk` | `scripts/upgrade.sh`——仅升级指定包，不触动其他依赖 |
| `uv run --with <pkg>` | CI 临时工具（`pytest-cov`、`pip-audit`），不修改冻结锁文件 |
| `uv run uvicorn …` | 开发服务器与 systemd `ExecStart` |

`uv` 二进制版本固定在 Dockerfile 中，保证镜像重新构建可复现；准确版本以 `Dockerfile` 为准。
