# Install muselab on Linux

One-shot installer for desktop / personal-server Linux. Runs as a **user-level
systemd service** — no root, no system-wide config, easy to undo.

## Prerequisites

- Linux with `systemd` (Ubuntu 18.04+, Debian 10+, Fedora 30+, Arch, …)
- `uv` ([install](https://docs.astral.sh/uv/getting-started/installation/)):
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- (For Anthropic models) `claude` CLI logged in once:
  ```bash
  claude login
  ```
  Non-Claude providers (DeepSeek / GLM / MiniMax / Kimi) only need API keys —
  set them in Settings UI later.

## Install

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
bash scripts/install-linux.sh
```

The script will:

1. Verify `uv` and `systemctl` are available
2. Run `uv sync` to install Python deps
3. **Ask you** for the archive directory (the only folder Muse can read/write),
   defaults to `~/muselab-archive`
4. Generate `.env` with a random `MUSELAB_TOKEN` and `MUSELAB_HOST=127.0.0.1`
5. Write `~/.config/systemd/user/muselab.service` and `systemctl --user enable --now`

If `.env` already exists, the script leaves it alone (re-running is safe).

## Verify

```bash
systemctl --user status muselab
xdg-open http://localhost:8765      # or just open in your browser
grep MUSELAB_TOKEN .env              # paste at login
```

## Survives reboot?

By default a user systemd service stops when you log out (or never starts if you
reboot and don't log in). Enable lingering once so muselab runs as long as the
machine is on:

```bash
sudo loginctl enable-linger $USER
```

Verify: `loginctl show-user $USER | grep Linger` → `Linger=yes`.

## Common commands

```bash
systemctl --user status   muselab     # current state
systemctl --user restart  muselab     # restart
systemctl --user stop     muselab     # stop without disabling
systemctl --user disable  muselab     # disable autostart (keeps unit file)
journalctl --user -u muselab -f       # tail logs
journalctl --user -u muselab -n 200   # last 200 lines
```

## Expose to LAN (optional)

Default binds to `127.0.0.1` only — your machine, your browser. To let phones /
tablets on the same WiFi connect:

1. Edit `.env`:
   ```
   MUSELAB_HOST=0.0.0.0
   ```
2. Open the firewall:
   ```bash
   sudo ufw allow 8765/tcp        # Ubuntu / Debian
   sudo firewall-cmd --add-port=8765/tcp --permanent && sudo firewall-cmd --reload  # Fedora / RHEL
   ```
3. Restart: `systemctl --user restart muselab`
4. From another device on the same WiFi: `http://<machine-ip>:8765`

⚠ Anyone on that network with the token has shell-level access to
`MUSELAB_ROOT`. For untrusted networks add HTTPS + nginx basic-auth on top.

## Uninstall

```bash
bash scripts/uninstall-linux.sh
```

Stops the service and deletes the unit file. `.env`, `sessions/`, and your
archive directory are **not** touched — delete the repo to remove fully.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `service failed to start` | `journalctl --user -u muselab -n 50` — usually missing `.env` value or port collision |
| Port already in use | `lsof -iTCP:8765 -sTCP:LISTEN` → kill the offender or change `MUSELAB_PORT` |
| Anthropic models 401 | `~/.claude` missing — run `claude login` once |
| Service stops after logout | enable lingering (see [Survives reboot?](#survives-reboot)) |
