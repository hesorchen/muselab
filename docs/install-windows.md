# Install muselab on Windows

One-shot installer for personal Windows machines. Uses **Task Scheduler** to
autostart on user logon. Runs entirely in user space — no service account, no
admin needed.

## Prerequisites

- Windows 10 or 11 (PowerShell 5+ included by default)
- `uv` ([install](https://docs.astral.sh/uv/getting-started/installation/)):
  ```powershell
  powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```
- (For Anthropic models) `claude` CLI logged in once:
  ```powershell
  claude login
  ```
  Stored under `%USERPROFILE%\.claude\`. Non-Claude providers (DeepSeek / GLM /
  MiniMax) just need API keys, set later in Settings UI.

## Install

```powershell
git clone https://github.com/hesorchen/muselab
cd muselab
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
```

> **Why `-ExecutionPolicy Bypass`?** Default Windows policy is `Restricted`
> which blocks unsigned scripts. `Bypass` applies only to this invocation; it
> does not change your system-wide policy.

The script will:

1. Verify `uv` (warn if `claude` missing)
2. Run `uv sync`
3. **Ask you** for the archive directory (where Muse can read/write),
   defaults to `%USERPROFILE%\muselab-archive`
4. Generate `.env` with a random `MUSELAB_TOKEN` and `MUSELAB_HOST=127.0.0.1`,
   then restrict the file ACL to your user
5. Register a Scheduled Task `Muselab` (trigger: At Logon, restart on failure)
6. Start the task immediately and curl `localhost:8765` to confirm

If `.env` already exists, the script keeps it (re-running is safe).

## Verify

```powershell
Get-ScheduledTask -TaskName Muselab    # State should be Running
Start-Process http://localhost:8765    # browser
Select-String MUSELAB_TOKEN .env       # paste at login
```

## Survives reboot?

Yes — task trigger is `AtLogOn`. Reboots that land you back at your user
account will autostart muselab. If you set up auto-login, it'll be running
before you sit down.

## Common commands

```powershell
Get-ScheduledTask    -TaskName Muselab     # check state
Start-ScheduledTask  -TaskName Muselab     # start
Stop-ScheduledTask   -TaskName Muselab     # stop
Get-Content -Wait "$env:LOCALAPPDATA\muselab\logs\stderr.log"   # tail logs
```

## Expose to LAN (optional)

Default binds to `127.0.0.1`. To reach it from your phone / tablet on the same
WiFi:

1. Edit `.env`:
   ```
   MUSELAB_HOST=0.0.0.0
   ```
2. Restart: `Stop-ScheduledTask -TaskName Muselab; Start-ScheduledTask -TaskName Muselab`
3. Find your machine's LAN IP: `ipconfig` → look for IPv4 under your WiFi adapter
4. Open Windows Firewall for port 8765 (PowerShell as admin):
   ```powershell
   New-NetFirewallRule -DisplayName "Muselab" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow
   ```
5. From another device: `http://<that-ip>:8765`

⚠ Token leak ≈ shell-level access to `MUSELAB_ROOT`. Don't expose without
HTTPS + auth layer on untrusted networks.

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File scripts\uninstall-windows.ps1
```

Stops and removes the scheduled task. `.env`, `sessions\`, your archive
directory, and logs are **not** touched.

## WSL alternative

If you prefer Linux tooling, install WSL2 + Ubuntu, then follow
[install-linux.md](install-linux.md) inside the WSL distro. The service runs
under `systemd --user` in WSL (`systemd.enabled = true` in `/etc/wsl.conf`).

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Task is `Ready` but never `Running` | Check `Last Run Result` in Task Scheduler GUI; usually a path issue. Verify `uv` is at `$UvPath` from the installer log. |
| Port already in use | `Get-NetTCPConnection -LocalPort 8765` → kill the PID or change `MUSELAB_PORT` |
| Anthropic models 401 | `claude login` in a fresh PowerShell |
| Logs empty | Task may not be running. `Get-ScheduledTask Muselab; Start-ScheduledTask Muselab` |
| Script signed-cert warning | Use `-ExecutionPolicy Bypass` (one-shot) instead of changing system policy |
