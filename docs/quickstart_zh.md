# Quick start

> [English](quickstart.md)

从 clone 到运行，三条命令。默认仅绑定 `127.0.0.1`，只有本机可访问；远程访问见
[VPS 部署](#vps-部署)。

## 0. 前置准备

### 至少配置一个模型 provider

| 你拥有的 | 配置方式 |
|----------------|-------|
| **Claude Pro / Max** 订阅 | 安装 [`claude` CLI](https://docs.claude.com/claude-code) 并执行一次 `claude login`，OAuth 凭据存于 `~/.claude/.credentials.json` |
| 仅想用第三方 key | 从 [DeepSeek](https://platform.deepseek.com) / [智谱 GLM](https://bigmodel.cn) / [MiniMax 国内站](https://minimaxi.com) 任取一个 key，安装完成后填到 Settings，无需 CLI |
| 两者都有 | Claude 跑硬推理，DeepSeek 跑日常对话。Dropdown 一键切换 |

未配置任何 provider 时安装仍然成功，但首次对话会失败。UI 会显示「未配置模型 —
请打开 Settings」，不会让你迷茫。

### 安装 `uv`

```bash
# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# Windows PowerShell — 干净 Windows 需三步一次性配置。
# 🚨 每一步之后必须关掉当前 PowerShell 窗口，从开始菜单重开一个 🚨
# Windows 进程启动时 snapshot PATH，不重开就用不上新装的工具
# 跳过重开 → 下一步会报「无法将 'git' / 'uv' 项识别为 cmdlet」

# (a) 放行 PowerShell 脚本（默认 Restricted 会拒绝 uv）
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser

# (b) 装 git（干净 Windows 不带）
winget install --id Git.Git -e

# (c) 装 uv
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## 1. 一键安装

登录后自动启动，默认绑定 localhost，耗时约 3 分钟（VPS 较慢时 10 分钟以上）。

```bash
# Linux / macOS
git clone https://github.com/hesorchen/muselab && cd muselab

bash scripts/install-macos.sh    # macOS — 用户级 LaunchAgent
bash scripts/install-linux.sh    # Linux — 用户级 systemd
```

```powershell
# Windows — Task Scheduler。PowerShell 5.1 不支持 && — 分两行执行。
git clone https://github.com/hesorchen/muselab
cd muselab
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
```

脚本流程：pre-flight 检查 → `uv sync` → 生成 `.env`（含随机 token）→ 7 项 intake
写入 CLAUDE.md → 注册自启 → 等待服务可用（最多 30s）。

## 2. 访问

本机：`http://localhost:8765` → 粘贴 `.env` 里的 token。

### VPS 部署

不要把端口直接暴露到公网。从笔记本建立 SSH tunnel：

```bash
ssh -L 8765:127.0.0.1:8765 your-vps-user@your-vps-host
# 然后在笔记本浏览器访问 http://localhost:8765
```

或用 [Tailscale](https://tailscale.com)——效果一致，不需要终端。

## 3. 验证

```bash
bash scripts/doctor.sh        # Linux / macOS
powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1   # Windows
```

`doctor` 会逐层检查（uv / claude CLI / `.env` / service / HTTP / token /
provider keys），失败时给出具体建议。运行异常时跑一下。

## 重启后会自启动吗？

| OS | 重启 → 重新登录 | 重启 → 不登录 |
|----|---------------------|------------------------|
| **macOS** | ✅ 自启 | n/a（Mac 重启必须登录）|
| **Linux** | ✅ 自启 | ⚠️ 需一次性执行 `sudo loginctl enable-linger $USER` |
| **Windows** | ✅ 自启 | n/a（Task Scheduler 触发器为 "At Logon"）|

各 OS 详细指南（验证 / 重启 / tail 日志 / 暴露 LAN / 卸载）：
[macOS](install-macos_zh.md) · [Linux](install-linux_zh.md) · [Windows](install-windows_zh.md)。

## Docker 备选方案

### GHCR 预构建镜像（多架构 amd64 + arm64）

```bash
docker run -d --name muselab \
  -p 8765:8765 \
  -e MUSELAB_TOKEN=$(openssl rand -hex 32) \
  -v $HOME/muselab-archive:/data \
  -e MUSELAB_ROOT=/data \
  -v $HOME/.claude:/home/muse/.claude \
  ghcr.io/hesorchen/muselab:latest
```

容器以非 root 用户 `muse` (uid 1000) 运行，home 在 `/home/muse/.claude`——
把宿主的 `~/.claude` 挂到那里就能复用 `claude login` 拿到的 OAuth 凭据。

> **宿主 UID 注意**：容器内 muse 用户是 uid 1000，大多数单用户 Linux / macOS
> 主机的账号也是 1000，bind-mount 直接能用。如果你的主机 UID 不是 1000
> （多用户机器、自定义 mac 管理员账号等），要么提前 `chmod -R go+rX ~/.claude`
> + `chown -R 1000:1000 ~/muselab-archive`，要么加 `--user $(id -u):$(id -g)`
> 但接受容器内 `~/.claude` 可能只读。

指定版本：`ghcr.io/hesorchen/muselab:1.2.3` / `:1.2` / `:sha-abc1234`。

### Docker Compose

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
cp .env.example .env && $EDITOR .env    # 填 MUSELAB_TOKEN、ARCHIVE_DIR
claude login                              # 宿主机执行，容器复用 OAuth
docker compose up -d
```

### 原生开发模式（uv，无 service）

```bash
cd muselab && uv sync
cp .env.example .env && $EDITOR .env
claude login
uv run python -m backend.main             # 绑定到 MUSELAB_HOST:MUSELAB_PORT
```
