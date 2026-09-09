# Quick start

> [English](quickstart.md)

从克隆到运行，共三条命令。默认仅绑定 `127.0.0.1`，只有本机可访问；远程访问方式见 [VPS 部署](#vps-部署)。

## 0. 环境要求

### 至少配置一个模型 provider

| 你拥有的 | 配置方式 |
|----------------|-------|
| **Claude Pro / Max** 订阅 | 安装 [`claude` CLI](https://docs.claude.com/claude-code) 并执行一次 `claude login`，OAuth 凭据存于 `~/.claude/.credentials.json` |
| 仅想用第三方 key | 从 [DeepSeek](https://platform.deepseek.com) / [智谱 GLM](https://bigmodel.cn) / [MiniMax](https://minimaxi.com) / [Kimi](https://platform.moonshot.cn) / [Qwen](https://dashscope.console.aliyun.com) 任取一个 key，安装完成后填到 Settings，无需 CLI |
| 两者都有 | Claude 用于高强度推理，DeepSeek 用于日常对话。下拉菜单一键切换 |

未配置任何模型提供商时安装仍然成功，但首次对话会失败，界面提示「未配置模型——请打开设置」。

### 安装 `uv`

```bash
# Linux / macOS / WSL2
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Windows 用户走 WSL2

Windows 上请通过 WSL2 安装。一次性配置：

```powershell
# PowerShell（管理员）
wsl --install            # 装 WSL2 + 默认 Ubuntu
# 按提示重启 + 创建 Linux 用户名 / 密码
```

当前新安装的 Ubuntu WSL 通常已启用 systemd。先检查用户管理器是否可达：

```bash
systemctl --user show-environment >/dev/null
```

若不可达，先检查用户会话；只有 systemd 未启用时才修改配置。保留已有内容：

```bash
if [ -f /etc/wsl.conf ]; then
  sudo cp -p /etc/wsl.conf "/etc/wsl.conf.muselab-backup-$(date +%Y%m%dT%H%M%S)"
fi
sudoedit /etc/wsl.conf
```

在已有 `[boot]` 节中设置 `systemd=true`，没有该节再添加；不要覆盖其他节。
修改后从 Windows PowerShell 执行 `wsl --shutdown`，再打开 WSL。
参见 [Microsoft systemd 文档](https://learn.microsoft.com/en-us/windows/wsl/systemd)。

再次打开 WSL 终端，从下面的一行命令安装。

## 1. 一键安装

登录后自动启动，默认绑定 localhost。普通机器约 3 分钟装好，低配 VPS 可能 10 分钟以上。

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

脚本执行流程：预检查 → `uv sync` → 选择主工作区 → 生成 `.env`（含随机 token
与稳定的绝对会话元数据路径）→ 注册自启动 → 等待服务就绪（最多 30 秒）。已有
`.env` 仅在缺少会话目录配置时补写，不覆盖自定义值。安装器不采集个人资料、
不创建预设目录，也不会自动写入 `CLAUDE.md`；Provider 在首次登录后的 Settings
中选择或配置。

## 2. 访问

本机：`http://localhost:8765` → 粘贴 `.env` 里的 token。

需要长期项目约定时，可选运行 `bash scripts/intake.sh` 生成通用工作区
`CLAUDE.md`；它不会创建固定目录。详见[配置工作区 CLAUDE.md](personalize-claude-md_zh.md)。

### VPS 部署

请勿将端口直接暴露到公网。从本地机器建立 SSH 隧道：

```bash
ssh -L 8765:127.0.0.1:8765 your-vps-user@your-vps-host
# 然后在笔记本浏览器访问 http://localhost:8765
```

或使用 [Tailscale](https://tailscale.com)——效果相同，无需命令行操作。

### 部署说明

muselab 面向单用户自托管场景。Agent 和终端使用服务账号的系统权限，多工作区不提供操作系统级隔离。远程访问应使用 SSH 隧道，或配置 HTTPS 与额外访问控制，详见[安全策略](../SECURITY.md)和[安全模型](backend-security_zh.md)。

工作文件与应用状态保存在部署机器上；使用云端模型、Embedding 或远程向量库时，相关数据会发送至所配置的服务。备份与恢复方法见[数据与备份](data-and-backup_zh.md)。

## 3. 验证

```bash
bash scripts/doctor.sh        # Linux / macOS / WSL2
```

`doctor` 会逐项检查（uv / claude CLI / `.env` / 服务状态 / HTTP / token / 模型密钥），出现故障时给出具体建议。

### 第一个任务

登录并配置模型后，先发送一条简单消息，再打开一个文件确认预览正常。然后选择项目所在的工作区，尝试一个完整任务：

> 「检查这个项目最近的改动，找出测试变慢的原因，修复后跑验证，并把结论写到 `docs/performance-note.md`。」

这类任务涉及读取代码与 Git diff、运行定向测试、修改文件和复验。执行过程中可以展开工具记录与代码差异；生成的说明文档可以直接在预览区打开，也可以通过真实终端独立复查。

代码、研究资料或知识库都可以作为工作区，不要求固定目录结构。工作区的项目上下文见[配置工作区 CLAUDE.md](personalize-claude-md_zh.md)，文件、会话与任务操作见[工作台操作](workbench-ui_zh.md)。

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

Linux 用户请先用 `id -u`／`id -g` 检查 UID／GID；若不是 1000，先按下方“挂载权限”构建匹配镜像，再执行启动命令。登录令牌保存在私有的 `~/.config/muselab/docker.env`，可在本机打开查看；不要公开该文件。

```bash
mkdir -p "$HOME/muselab-workspace" "$HOME/muselab-sessions" "$HOME/.claude" "$HOME/.config/muselab"
if [ ! -f "$HOME/.config/muselab/docker.env" ]; then
  (umask 077; set -C; printf 'MUSELAB_TOKEN=%s\n' "$(openssl rand -hex 32)" > "$HOME/.config/muselab/docker.env")
fi
docker run -d --name muselab \
  -p 127.0.0.1:8765:8765 \
  --env-file "$HOME/.config/muselab/docker.env" \
  -v "$HOME/muselab-workspace:/data" \
  -v "$HOME/muselab-sessions:/app/sessions" \
  -v "$HOME/.claude:/home/muse/.claude" \
  ghcr.io/hesorchen/muselab:latest
```

挂载 `/app/sessions` 可保留会话索引、注解、队列、`config/` 中的网页配置，以及 `state/muselab/vendor-cli/` 中的第三方会话正文。网页保存的配置覆盖 Compose／env-file 的初始同名值。旧镜像用户须在替换容器前完成[旧状态迁移](docker-state-migration_zh.md)。

> **绑定地址说明：** 上面示例显式绑定 `127.0.0.1`，服务只在本机可达。直接写 `-p 8765:8765` 会绑到 `0.0.0.0`（所有网卡）——在公网 VPS 上等于把服务挂到互联网上，只靠 token 一道防线。若需 LAN 内访问（如手机连本机），改成 `-p 0.0.0.0:8765:8765`，并务必在前面加防火墙或反向代理。仓库自带的 `docker-compose.yml` 默认绑 `127.0.0.1`，要放开在 `.env` 设 `MUSELAB_BIND=0.0.0.0`。

容器以非 root 用户 `muse`（uid 1000）运行，主目录为 `/home/muse/.claude`。将宿主机的 `~/.claude` 挂载至该路径，即可复用 `claude login` 获取的 OAuth 凭据。

**挂载权限。** 发布镜像默认使用 UID／GID 1000。Linux 宿主机上的工作区、
会话目录和 Claude 状态目录必须允许该用户读写。不要递归向其他用户开放
`~/.claude`，其中包含 OAuth 凭据和会话记录。刷新 token 和持久化会话都需要
写权限，只读挂载不能满足这些操作。

若目录所有者不同，使用匹配当前非 root UID／GID 的源码 Compose 构建。
先以自己的账号创建宿主机目录，再让 Docker 挂载：

```bash
mkdir -p data sessions
MUSELAB_UID=$(id -u) MUSELAB_GID=$(id -g) docker compose up -d --build
```

在 Compose 使用的 `.env` 中保存 `MUSELAB_UID` 和 `MUSELAB_GID` 可保留此选择。
Docker Desktop 应验证实际挂载权限，不要假设宿主机账号是 UID 1000。
也可以为本应用准备独立的 Claude 状态目录，隔离登录与会话；该目录仍须保持
私有且可写。这些方式不要求修改原有凭据文件的权限。

指定版本：`ghcr.io/hesorchen/muselab:1.2.3` / `:1.2` / `:sha-abc1234`。

### Docker Compose

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
cp .env.example .env && $EDITOR .env    # 填 MUSELAB_TOKEN、ARCHIVE_DIR（宿主机工作区，兼容变量名）
claude login                              # 宿主机执行，容器复用 OAuth
mkdir -p data sessions
docker compose up -d --build
```

### 原生开发模式（uv，无 service）

```bash
cd muselab && uv sync
cp .env.example .env && $EDITOR .env
claude login
uv run python -m backend.main             # 绑定到 MUSELAB_HOST:MUSELAB_PORT
```
