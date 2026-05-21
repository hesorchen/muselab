# Quick start

> [简体中文](quickstart_zh.md)

From clone to running, in three commands. Default bind is `127.0.0.1`, so it
is only reachable from the same machine until you choose a remote-access
pattern (see [SSH tunnel](#vps) below).

## 0. Prerequisites

### Pick at least one model provider

| If you have… | Setup |
|----------------|-------|
| **Claude Pro / Max** subscription | Install [`claude` CLI](https://docs.claude.com/claude-code) then run `claude login` once. OAuth lives in `~/.claude/.credentials.json` |
| Just want a cheap key | Get one from [DeepSeek](https://platform.deepseek.com) / [智谱 GLM](https://bigmodel.cn) / [MiniMax 国内站](https://minimaxi.com). Paste it in Settings after install — no CLI required |
| Both | Use Claude for hard reasoning, DeepSeek for cheap. Switch model in a dropdown click |

Without any provider configured, muselab installs fine but the first chat
will error. The UI explicitly says "no provider configured — open Settings"
so you won't be left confused.

### Install `uv`

```bash
# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# Windows PowerShell — clean Windows needs three one-time setups.
# 🚨 CLOSE + REOPEN POWERSHELL AFTER EACH STEP so PATH refreshes 🚨
# (Skip the reopen and the next step will fail with "'git' / 'uv' is not recognized")

# (a) allow scripts to run (default policy is Restricted)
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser

# (b) install git (default Windows doesn't have it)
winget install --id Git.Git -e

# (c) install uv
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## 1. One-shot installer

Autostart at login, localhost-only, ~3 min on a decent machine (10+ on slow VPS).

```bash
# Linux / macOS
git clone https://github.com/hesorchen/muselab && cd muselab

bash scripts/install-macos.sh    # macOS — user LaunchAgent
bash scripts/install-linux.sh    # Linux — user systemd service
```

```powershell
# Windows — Task Scheduler. PowerShell 5.1 doesn't support && — run as two lines.
git clone https://github.com/hesorchen/muselab
cd muselab
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
```

What it does: pre-flight checks → `uv sync` → write `.env` with random token →
7-question profile intake → register autostart → wait up to 30s for service.

## 2. Open it

Local machine: `http://localhost:8765` → paste the token from `.env`.

### VPS

Don't open the port to the internet. SSH tunnel from your laptop:

```bash
ssh -L 8765:127.0.0.1:8765 your-vps-user@your-vps-host
# then visit http://localhost:8765 in your laptop's browser
```

Or use [Tailscale](https://tailscale.com) — same effect, no terminal.

## 3. Verify

```bash
bash scripts/doctor.sh        # Linux / macOS
powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1   # Windows
```

`doctor` checks every layer (uv / claude CLI / `.env` / service / HTTP /
token / provider keys) and gives specific advice on any failure. Run it
when something feels off.

## Survives reboot?

| OS | Reboot → log back in | Reboot → never log in |
|----|---------------------|------------------------|
| **macOS** | ✅ auto-starts | n/a (always log in on Mac) |
| **Linux** | ✅ auto-starts | ⚠️ needs one-time `sudo loginctl enable-linger $USER` |
| **Windows** | ✅ auto-starts | n/a (Task Scheduler is "At Logon") |

Per-OS detail (verify / restart / tail logs / expose to LAN / uninstall):
[macOS](install-macos.md) · [Linux](install-linux.md) · [Windows](install-windows.md).

## Docker alternative

### Pre-built image from GHCR (multi-arch amd64 + arm64)

```bash
docker run -d --name muselab \
  -p 8765:8765 \
  -e MUSELAB_TOKEN=$(openssl rand -hex 32) \
  -v $HOME/muselab-archive:/data \
  -e MUSELAB_ROOT=/data \
  -v $HOME/.claude:/home/muse/.claude \
  ghcr.io/hesorchen/muselab:latest
```

The container runs as a non-root `muse` user (uid 1000), so its home is
`/home/muse/.claude` — bind-mount your host's `~/.claude` there to reuse
the OAuth credentials from `claude login`.

Pin a version: `ghcr.io/hesorchen/muselab:1.2.3` / `:1.2` / `:sha-abc1234`.

### Docker Compose

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
cp .env.example .env && $EDITOR .env    # set MUSELAB_TOKEN, ARCHIVE_DIR
claude login                              # host-side; container reuses OAuth
docker compose up -d
```

### Native dev (uv, no service)

```bash
cd muselab && uv sync
cp .env.example .env && $EDITOR .env
claude login
uv run python -m backend.main             # binds MUSELAB_HOST:MUSELAB_PORT
```
