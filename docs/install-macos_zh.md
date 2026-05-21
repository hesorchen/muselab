# 在 macOS 上安装 muselab

> [English](install-macos.md)

个人 Mac 一键安装。作为**用户级 LaunchAgent** 运行——无需 `sudo`，登录后
自动启动，崩溃自动重启。

## 前置准备

- macOS 12（Monterey）或更高（Apple Silicon 或 Intel）
- `uv`（[安装文档](https://docs.astral.sh/uv/getting-started/installation/)）：
  ```bash
  brew install uv
  # 或：  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- （需要 Anthropic 模型时）`claude` CLI 登录过一次：
  ```bash
  claude login
  ```
  会把 OAuth 写到 `~/.claude/`，agent 复用之。非 Claude provider（DeepSeek /
  GLM / MiniMax）只需 API key——稍后在 Settings UI 里填。

## 安装

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
bash scripts/install-macos.sh
```

脚本会：

1. 校验 `uv`（缺 `claude` 会警告）
2. 执行 `uv sync`
3. **询问你**的 archive 目录（Muse 可读写的目录），默认 `~/muselab-archive`
4. 生成 `.env`（含随机 `MUSELAB_TOKEN` 和 `MUSELAB_HOST=127.0.0.1`）
5. 写入 `~/Library/LaunchAgents/com.muselab.plist` 并 `launchctl load -w`
6. curl `localhost:8765` 确认服务可用

如果 `.env` 已存在，脚本会保留不动（可安全重跑）。

## 验证

```bash
launchctl list | grep muselab        # 应该有 PID
open http://localhost:8765            # 浏览器
grep MUSELAB_TOKEN .env               # 登录时粘贴
```

## 重启后会自动启动吗？

会——plist 内 `RunAtLoad=true`。macOS 登录时自动启动 agent。不需要像 Linux
那样的额外 `loginctl enable-linger` 设置。

如果你想**登录前**就启动（罕见场景，比如 headless Mac mini），把它从
`LaunchAgents` 移到 `LaunchDaemons` 并以 root 运行——超出本 installer 范围；
需要时联系作者。

## 常用命令

```bash
launchctl list | grep muselab                          # 查是否已加载
launchctl kickstart -k gui/$UID/com.muselab            # 重启（保留状态）
launchctl unload  ~/Library/LaunchAgents/com.muselab.plist   # 停止（下次登录前不再启动）
launchctl load -w ~/Library/LaunchAgents/com.muselab.plist   # 再次启动
tail -f ~/Library/Logs/muselab/stderr.log              # tail 日志

bash scripts/doctor.sh                                  # 重新校验安装并探测服务
bash scripts/intake.sh                                  # 重做 profile intake / 刷新 CLAUDE.md
```

## 暴露到 LAN（可选）

默认仅绑定 `127.0.0.1`。让同一 WiFi 的手机 / iPad 能连：

1. 编辑 `.env`：
   ```
   MUSELAB_HOST=0.0.0.0
   ```
2. 重启：`launchctl kickstart -k gui/$UID/com.muselab`
3. 查 Mac 的 LAN IP：`ipconfig getifaddr en0`（WiFi）或 `en1`（以太）
4. 在其他设备上访问：`http://<那个 IP>:8765`

macOS 防火墙：System Settings → Network → Firewall。如果开启了，可能会弹
"accept incoming connections" 询问 `python`——允许即可。

⚠ Token 泄露 ≈ shell 访问权限。不可信网络上不要不加 HTTPS + 认证层
（nginx basic-auth、Tailscale 等）直接暴露。

## 卸载

```bash
bash scripts/uninstall-macos.sh
```

卸载并删除 plist。`.env`、`sessions/`、archive 目录、日志目录**不会**被动。
彻底删除请直接删除仓库。

## 排错

| 现象 | 排查 |
|------|------|
| `agent failed to load` | `cat ~/Library/Logs/muselab/stderr.log`——通常是 `.env` 缺失或不合法 |
| 端口被占 | `lsof -iTCP:8765 -sTCP:LISTEN` → 杀进程或改 `MUSELAB_PORT` |
| Anthropic 模型 401 | `~/.claude` 缺失或过期——再 `claude login` 一次 |
| agent 找不到 `claude` | plist 内 `PATH` 硬编码了 `/opt/homebrew/bin` 和 `/usr/local/bin`。如果你的 `claude` 在别处，编辑 plist 的 `EnvironmentVariables/PATH`，然后 `launchctl unload && load -w` |
| plist 已安装但登录后不自启 | `launchctl print gui/$UID/com.muselab`，查 `state = running` |
