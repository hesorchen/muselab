# Quick start

> [English](quickstart.md)

从克隆到运行，共三条命令。默认仅绑定 `127.0.0.1`，只有本机可访问；远程访问方式见 [VPS 部署](#vps-部署)。

## 0. 前置准备

### 至少配置一个模型 provider

| 你拥有的 | 配置方式 |
|----------------|-------|
| **Claude Pro / Max** 订阅 | 安装 [`claude` CLI](https://docs.claude.com/claude-code) 并执行一次 `claude login`，OAuth 凭据存于 `~/.claude/.credentials.json` |
| 仅想用第三方 key | 从 [DeepSeek](https://platform.deepseek.com) / [智谱 GLM](https://bigmodel.cn) / [MiniMax](https://minimaxi.com) / [Kimi](https://platform.moonshot.cn) / [Qwen](https://dashscope.console.aliyun.com) 任取一个 key，安装完成后填到 Settings，无需 CLI |
| 两者都有 | Claude 用于高强度推理，DeepSeek 用于日常对话。下拉菜单一键切换 |

未配置任何模型提供商时安装仍然成功，但首次对话请求会失败。界面会显示「未配置模型——请打开设置」，说明故障原因。

### 安装 `uv`

```bash
# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# Windows PowerShell — 干净的 Windows 系统需要三步一次性配置。
# 每一步完成后必须关闭当前 PowerShell 窗口并重新打开，以使 PATH 更新生效。
# Windows 进程启动时会快照 PATH，不重新打开则新安装的工具不可用。
# 跳过重新打开步骤，下一步会报「无法将 'git' / 'uv' 识别为 cmdlet」。

# (a) 放行 PowerShell 脚本（默认 Restricted 会拒绝 uv）
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser

# (b) 装 git（干净 Windows 不带）
winget install --id Git.Git -e

# (c) 装 uv
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## 1. 一键安装

登录后自动启动，默认绑定 localhost，耗时约 3 分钟（低配 VPS 可能需要 10 分钟以上）。

### 1a. 一行命令引导（Linux + macOS + WSL2）

自动安装 `uv`，将仓库克隆至 `~/muselab`，再调用平台安装程序完成全部安装。首次安装推荐使用此方式：

```bash
curl -fsSL https://raw.githubusercontent.com/hesorchen/muselab/main/scripts/quick-install.sh | bash
```

如需在执行前审查脚本内容，可先下载后再运行：

```bash
curl -fsSL https://raw.githubusercontent.com/hesorchen/muselab/main/scripts/quick-install.sh -o quick-install.sh
less quick-install.sh   # 看一遍
bash quick-install.sh
```

### 1b. 手动安装（逐步执行）

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

脚本执行流程：预检查 → `uv sync` → 生成 `.env`（含随机 token）→ 7 项问答写入 CLAUDE.md → 注册自启动 → 等待服务可用（最多 30 秒）。

## 2. 访问

本机：`http://localhost:8765` → 粘贴 `.env` 里的 token。

### VPS 部署

请勿将端口直接暴露到公网。从本地机器建立 SSH 隧道：

```bash
ssh -L 8765:127.0.0.1:8765 your-vps-user@your-vps-host
# 然后在笔记本浏览器访问 http://localhost:8765
```

或使用 [Tailscale](https://tailscale.com)——效果相同，无需命令行操作。

## 3. 验证

```bash
bash scripts/doctor.sh        # Linux / macOS
powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1   # Windows
```

`doctor` 会逐层检查（uv / claude CLI / `.env` / 服务状态 / HTTP / token / 模型密钥），出现故障时给出具体建议。遇到异常时执行此命令进行诊断。

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

容器以非 root 用户 `muse`（uid 1000）运行，主目录为 `/home/muse/.claude`。将宿主机的 `~/.claude` 挂载至该路径，即可复用 `claude login` 获取的 OAuth 凭据。

> **宿主机 UID 说明：** 容器内 muse 用户为 uid 1000，大多数单用户 Linux / macOS 主机的账号也是 uid 1000，挂载可直接生效。若宿主机 UID 不同（多用户环境、自定义 macOS 管理员账号等），需在启动容器前执行 `chmod -R go+rX ~/.claude` 及 `chown -R 1000:1000 ~/muselab-archive`；或传入 `--user $(id -u):$(id -g)`，但需接受容器内 `~/.claude` 可能为只读。

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
