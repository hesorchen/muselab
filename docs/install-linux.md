# Install muselab on Linux

> [简体中文](install-linux_zh.md)

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
  Most non-Claude providers (DeepSeek / GLM / MiniMax / Kimi / Qwen /
  Xiaomi MiMo / Baidu Qianfan (ERNIE)) only need API keys — paste them in the
  Settings UI after install, no CLI needed. Codex Gateway needs a local sidecar
  and local token; see [Codex Gateway](codex-gateway.md).

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

bash scripts/doctor.sh                # re-verify install + probe service
bash scripts/intake.sh                # (re)run profile intake / update CLAUDE.md
```

## Re-run intake / refresh profile

The 7-question profile intake from the installer can be re-run any time:

```bash
bash scripts/intake.sh
```

Useful after life changes (job / move / new family member) or if you skipped
intake at install time. Existing `CLAUDE.md` gets backed up to `CLAUDE.md.bak`
before overwrite.

## Verify install / debug weirdness

```bash
bash scripts/doctor.sh
```

Probes uv / claude CLI / `.env` / service state / HTTP / token / provider keys layer by layer. Returns non-zero on blocking failures.

## Accessing from your laptop when muselab runs on a VPS

Default binds to `127.0.0.1:8765` — **deliberately**. Your VPS port 8765 is
NOT reachable from your laptop's browser even if the firewall is open. Three
ways to actually use it:

### A. SSH tunnel (recommended — zero extra config)

On your **laptop**:

```bash
ssh -L 8765:127.0.0.1:8765 your-vps-user@your-vps-host
```

Keep that terminal open. Now `http://localhost:8765` in your laptop's
browser hits the muselab on the VPS. No firewall opens, no auth exposure,
no extra moving parts.

### B. Tailscale / WireGuard (best for "always on" remote)

Put your VPS and laptop in the same Tailscale net, then visit
`http://<vps-tailscale-ip>:8765`. The tunnel is end-to-end encrypted and
auth'd by Tailscale, so binding 127.0.0.1 is fine.

### C. Bind to LAN (only if you trust the network) — see below

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
