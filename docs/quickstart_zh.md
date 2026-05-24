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
# Linux / macOS / WSL2
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Windows 用户走 WSL2

muselab 不支持 Windows 原生（systemd / launchd 是 Linux/macOS 的服务模型，
Task Scheduler 走另一套；维持双轨成本过高）。Windows 用户的路径是 WSL2 —— 微软
官方的 Linux 子系统，体验和 Linux 一致。一次性配置：

```powershell
# PowerShell（管理员）
wsl --install            # 装 WSL2 + 默认 Ubuntu
# 重启电脑，按提示创建 WSL 内的 Linux 用户名 / 密码
```

WSL2 默认不带 systemd，muselab 服务注册需要它。在 WSL 内：

```bash
sudo tee /etc/wsl.conf >/dev/null <<'EOF'
[boot]
systemd=true
EOF
```

回到 Windows 端 PowerShell：

```powershell
wsl --shutdown           # 让 wsl.conf 生效
```

再次打开 WSL 终端，从下面的一行命令安装即可。

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
# Linux / macOS / WSL2
git clone https://github.com/hesorchen/muselab && cd muselab

bash scripts/install-macos.sh    # macOS — 用户级 LaunchAgent
bash scripts/install-linux.sh    # Linux / WSL2 — 用户级 systemd
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
bash scripts/doctor.sh        # Linux / macOS / WSL2
```

`doctor` 会逐层检查（uv / claude CLI / `.env` / 服务状态 / HTTP / token / 模型密钥），出现故障时给出具体建议。遇到异常时执行此命令进行诊断。

## 重启后会自启动吗？

| OS | 重启 → 重新登录 | 重启 → 不登录 |
|----|---------------------|------------------------|
| **macOS** | ✅ 自启 | n/a（Mac 重启必须登录）|
| **Linux** | ✅ 自启 | ⚠️ 需一次性执行 `sudo loginctl enable-linger $USER` |
| **WSL2** | ✅ 自启（打开 WSL 终端即触发 systemd-user） | ⚠️ Windows 重启后需手动打开一次 WSL 终端，或参考 [WSL boot 配置](https://learn.microsoft.com/en-us/windows/wsl/wsl-config) |

各 OS 详细指南（验证 / 重启 / tail 日志 / 暴露 LAN / 卸载）：
[macOS](install-macos_zh.md) · [Linux](install-linux_zh.md)。

## Docker 备选方案

### GHCR 预构建镜像（多架构 amd64 + arm64）

```bash
docker run -d --name muselab \
  -p 127.0.0.1:8765:8765 \
  -e MUSELAB_TOKEN=$(openssl rand -hex 32) \
  -v $HOME/muselab-archive:/data \
  -e MUSELAB_ROOT=/data \
  -v $HOME/.claude:/home/muse/.claude \
  ghcr.io/hesorchen/muselab:latest
```

> **绑定地址说明：** 上面示例显式绑定 `127.0.0.1`，服务只在本机可达。直接写 `-p 8765:8765` 会绑到 `0.0.0.0`（所有网卡）——在公网 VPS 上等于把服务挂到互联网上，只靠 token 一道防线。若需 LAN 内访问（如手机连本机），改成 `-p 0.0.0.0:8765:8765`，并务必在前面加防火墙或反向代理。仓库自带的 `docker-compose.yml` 默认绑 `127.0.0.1`，要放开在 `.env` 设 `MUSELAB_BIND=0.0.0.0`。

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
