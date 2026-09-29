# 为 SSH 部署拆分独立上传通道

> [English](ssh-upload-tunnel.md)

反向代理通过一条 SSH 连接访问 MuseLab 时，上传与聊天请求会共用该连接。
上传数据积压或重传会延迟健康检查和 SSE 请求。独立 SSH 连接可以隔离上传队列，
底层网络的重传仍需要单独处理。

在 Linux 反向代理主机上保留现有连接，另安装
`scripts/ssh-upload-tunnel.sh`：

```bash
install -d -m 700 ~/.local/bin ~/.config/muselab ~/.config/systemd/user
install -m 755 scripts/ssh-upload-tunnel.sh ~/.local/bin/muselab-upload-tunnel
install -m 644 examples/muselab-upload-tunnel.service ~/.config/systemd/user/
```

创建权限为 `600` 的 `~/.config/muselab/upload-tunnel.env`：

```ini
MUSELAB_UPLOAD_SSH_HOST=app-host
MUSELAB_UPLOAD_LISTEN_PORT=8785
MUSELAB_UPLOAD_TARGET_PORT=8765
MUSELAB_UPLOAD_TARGET_HOST=127.0.0.1
```

`app-host` 可以是 SSH 配置别名或可达主机；在 SSH 配置中设置用户、密钥、端口，
并预先核对主机密钥。也可在环境文件中设置 `MUSELAB_UPLOAD_SSH_USER` 和
`MUSELAB_UPLOAD_SSH_IDENTITY_FILE`。本地监听固定绑定 `127.0.0.1`。

若应用位于 Windows 上的 WSL2 NAT 网络，另添加：

```ini
MUSELAB_UPLOAD_TARGET_COMMAND="wsl.exe -d Ubuntu -- hostname -I"
```

脚本在启动时发现并校验首个 IPv4 地址，用 GNU `timeout` 限制发现过程的等待时间。
目标端口必须与原连接访问的应用端口相同。脚本显式禁用 SSH 配置中的连接复用。

```bash
systemctl --user daemon-reload
systemctl --user enable --now muselab-upload-tunnel.service
```

按现有站点调整 [Caddy 示例](../examples/Caddyfile.ssh-upload-tunnels)，保留原有鉴权、
安全响应头和其他站点配置。只有 `POST /api/files/upload` 与
`POST /api/chat/upload-image` 使用 `8785`；提交保存、取消和大小限制查询仍走原连接。
两个上游必须连接同一个应用实例，才能提交已暂存的上传。上传连接不可用时仅让上传失败，
不要回退到原连接。

校验完整 Caddy 配置后再 reload。切换路由无需重启 MuseLab 或现有 SSH 连接。

验收时执行带鉴权的上传、提交和下载校验，比较文件哈希，同时观察原连接上的健康请求
和 SSE。也测试取消和上传连接故障；记录两个 SSH PID／连接，确认应用和原连接 PID
未变化。回滚时先恢复旧代理路由，再停止上传服务。

参考：[SSH 通道复用](https://www.rfc-editor.org/info/rfc4254/#section-5)、
[OpenSSH ControlPath](https://man.openbsd.org/ssh_config#ControlPath)、
[Caddy 互斥路由](https://caddyserver.com/docs/caddyfile/directives/handle)。
