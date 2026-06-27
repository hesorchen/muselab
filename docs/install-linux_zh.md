# 在 Linux 上安装 muselab

> [English](install-linux.md)

桌面 / 个人服务器 Linux 一键安装。作为**用户级 systemd 服务**运行——
无需 root，无系统级配置，容易撤销。

## 环境要求

- 带 `systemd` 的 Linux（Ubuntu 18.04+ / Debian 10+ / Fedora 30+ / Arch / …）
- `uv`（[安装文档](https://docs.astral.sh/uv/getting-started/installation/)）：
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- （需要 Anthropic 模型时）`claude` CLI 登录过一次：
  ```bash
  claude login
  ```
  多数非 Claude provider（DeepSeek / GLM / MiniMax / Kimi / Qwen / 小米 MiMo / 百度千帆（ERNIE））只需 API key——稍后在 Settings UI 内填即可，无需 CLI。Codex Gateway 需要本地 sidecar 和本地 token，见 [Codex Gateway](codex-gateway_zh.md)。

## 安装

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
bash scripts/install-linux.sh
```

脚本会：

1. 校验 `uv` 和 `systemctl` 可用
2. 执行 `uv sync` 安装 Python 依赖
3. **询问你**的 archive 目录（Muse 可读写的文件夹），默认 `~/muselab-archive`
4. 生成 `.env`（含随机 `MUSELAB_TOKEN` 和 `MUSELAB_HOST=127.0.0.1`）
5. 写入 `~/.config/systemd/user/muselab.service` 并执行 `systemctl --user enable --now`

如果 `.env` 已存在，脚本会保留不动（可安全重跑）。

## 验证

```bash
systemctl --user status muselab
xdg-open http://localhost:8765      # 或直接在浏览器打开
grep MUSELAB_TOKEN .env              # 登录时粘贴 token
```

## 重启后会自动启动吗？

默认情况下，user systemd 服务在你登出时会停止（重启后未登录也不会启动）。
执行一次 lingering 就能让 muselab 在机器开机时常驻：

```bash
sudo loginctl enable-linger $USER
```

验证：`loginctl show-user $USER | grep Linger` → `Linger=yes`。

## 常用命令

```bash
systemctl --user status   muselab     # 查看状态
systemctl --user restart  muselab     # 重启
systemctl --user stop     muselab     # 停止（不取消自启）
systemctl --user disable  muselab     # 取消自启（保留 unit 文件）
journalctl --user -u muselab -f       # tail 日志
journalctl --user -u muselab -n 200   # 最近 200 行

bash scripts/doctor.sh                # 重新校验安装并探测服务
bash scripts/intake.sh                # 重做 profile intake / 更新 CLAUDE.md
```

## 重做 intake / 刷新档案

installer 的 7 问 intake 可以随时重跑：

```bash
bash scripts/intake.sh
```

生活变化（换工作 / 搬家 / 新增家庭成员）后或安装时跳过了 intake 时很有用。
现有 `CLAUDE.md` 会在覆盖前备份到 `CLAUDE.md.bak`。

## 校验安装 / 调试异常

```bash
bash scripts/doctor.sh
```

逐项检查 uv / claude CLI / `.env` / 服务状态 / HTTP / token / provider key，阻塞性失败时返回非零。

## VPS 上跑 muselab 时如何从笔记本访问

默认仅绑定 `127.0.0.1:8765`——**有意为之**。即使防火墙开了，你的 VPS 上的 8765
端口对笔记本浏览器**不可达**。三种实际可用的方式：

### A. SSH tunnel（推荐——零额外配置）

在**笔记本**上：

```bash
ssh -L 8765:127.0.0.1:8765 your-vps-user@your-vps-host
```

保持终端不关。然后在笔记本浏览器访问 `http://localhost:8765` 就能命中 VPS 上的
muselab。不开防火墙、不暴露认证、零额外组件。

### B. Tailscale / WireGuard（适合「常驻」远程）

把 VPS 和笔记本加入同一个 Tailscale 网络，访问
`http://<vps-tailscale-ip>:8765`。tunnel 端到端加密，由 Tailscale 提供认证，
所以绑定 127.0.0.1 没问题。

### C. 绑定到 LAN（仅在你完全信任网络时） — 见下文

## 暴露到 LAN（可选）

默认仅绑定 `127.0.0.1`——你自己的浏览器。让同一 WiFi 的手机 / 平板能连：

1. 编辑 `.env`：
   ```
   MUSELAB_HOST=0.0.0.0
   ```
2. 开防火墙：
   ```bash
   sudo ufw allow 8765/tcp        # Ubuntu / Debian
   sudo firewall-cmd --add-port=8765/tcp --permanent && sudo firewall-cmd --reload  # Fedora / RHEL
   ```
3. 重启：`systemctl --user restart muselab`
4. 在同 WiFi 的设备上访问：`http://<machine-ip>:8765`

⚠ 网络里任何拿到 token 的人都对 `MUSELAB_ROOT` 有 shell 级访问权限。
不可信网络上请在前面加 HTTPS + nginx basic-auth。

## 卸载

```bash
bash scripts/uninstall-linux.sh
```

停止服务并删除 unit 文件。`.env`、`sessions/`、archive 目录**不会**被动。
彻底删除请直接删除仓库。

## 排错

| 现象 | 排查 |
|------|------|
| `service failed to start` | `journalctl --user -u muselab -n 50`——通常是 `.env` 缺值或端口冲突 |
| 端口被占 | `lsof -iTCP:8765 -sTCP:LISTEN` → 杀进程或改 `MUSELAB_PORT` |
| Anthropic 模型 401 | `~/.claude/.credentials.json` 缺失——执行一次 `claude login` |
| 登出后服务停止 | 开启 lingering（见[重启后会自动启动吗？](#重启后会自动启动吗)）|
