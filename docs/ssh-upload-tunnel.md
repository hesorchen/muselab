# Separate uploads from an SSH control tunnel

> [简体中文](ssh-upload-tunnel_zh.md)

When a reverse proxy reaches MuseLab through one SSH connection, upload traffic
and chat requests share that connection. Retransmissions and queued upload data
can delay health checks and SSE requests. A separate SSH connection isolates the
upload queue. It does not repair packet loss in the underlying network.

On the Linux reverse-proxy host, keep the existing control tunnel and install
[ssh-upload-tunnel.sh](../scripts/ssh-upload-tunnel.sh) as a second service:

```bash
install -d -m 700 ~/.local/bin ~/.config/muselab ~/.config/systemd/user
install -m 755 scripts/ssh-upload-tunnel.sh ~/.local/bin/muselab-upload-tunnel
install -m 644 examples/muselab-upload-tunnel.service ~/.config/systemd/user/
```

Create `~/.config/muselab/upload-tunnel.env` with mode `600`:

```ini
MUSELAB_UPLOAD_SSH_HOST=app-host
MUSELAB_UPLOAD_LISTEN_PORT=8785
MUSELAB_UPLOAD_TARGET_PORT=8765
MUSELAB_UPLOAD_TARGET_HOST=127.0.0.1
```

`app-host` is an SSH config alias or reachable host. Configure its user, key,
port and verified host key in SSH config. Alternatively, set
`MUSELAB_UPLOAD_SSH_USER` and `MUSELAB_UPLOAD_SSH_IDENTITY_FILE` in the environment
file. The listener is always bound to `127.0.0.1`.

For Windows-hosted WSL2 NAT, set this additional entry:

```ini
MUSELAB_UPLOAD_TARGET_COMMAND="wsl.exe -d Ubuntu -- hostname -I"
```

The script discovers the first IPv4 address at startup, validates it, and bounds
discovery with GNU `timeout`. The target port must be the same application port
used by the control tunnel. SSH config connection reuse is explicitly disabled.

```bash
systemctl --user daemon-reload
systemctl --user enable --now muselab-upload-tunnel.service
```

Adapt [the Caddy example](../examples/Caddyfile.ssh-upload-tunnels) to the existing
site. Preserve the site's authentication, security headers and other routes.
Only `POST /api/files/upload` and `POST /api/chat/upload-image` use port `8785`;
upload commit, cancel and size-limit requests remain on the control connection.
Both upstreams must reach the same application instance so staged uploads can
be committed. An unavailable upload tunnel fails uploads; it must not fall back
to the control tunnel.

Validate the complete Caddy configuration before reloading it. Reloading this
route does not require restarting MuseLab or the existing SSH tunnel.

Verify an authenticated upload, commit and downloaded-file hash while observing
health checks and SSE through the control upstream. Test cancellation and a
failed upload connection too. Record the two SSH PIDs/connections and confirm
the application and control-tunnel PIDs remain unchanged. For rollback, restore
the previous proxy route first, then stop the upload service.

References: [SSH channel multiplexing](https://www.rfc-editor.org/info/rfc4254/#section-5),
[OpenSSH ControlPath](https://man.openbsd.org/ssh_config#ControlPath),
[Caddy mutually exclusive handlers](https://caddyserver.com/docs/caddyfile/directives/handle).
