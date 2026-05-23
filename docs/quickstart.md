# Quick start

> [简体中文](quickstart_zh.md)

From clone to running in three commands. The default bind address is `127.0.0.1`,
so the service is only reachable from the local machine until you configure a
remote-access method (see [SSH tunnel](#vps) below).

## 0. Prerequisites

### Pick at least one model provider

| If you have… | Setup |
|----------------|-------|
| **Claude Pro / Max** subscription | Install [`claude` CLI](https://docs.claude.com/claude-code) then run `claude login` once. OAuth lives in `~/.claude/.credentials.json` |
| Just want a cheap key | Get one from [DeepSeek](https://platform.deepseek.com) / [智谱 GLM](https://bigmodel.cn) / [MiniMax](https://minimaxi.com) / [Kimi](https://platform.moonshot.cn) / [Qwen](https://dashscope.console.aliyun.com). Paste it in Settings after install — no CLI required |
| Both | Use Claude for demanding reasoning tasks, DeepSeek for cost-sensitive workloads. Switch models with a single dropdown click |

Without any provider configured, muselab installs successfully but the first
chat request will fail. The UI displays "no provider configured — open Settings"
to make the cause clear.

### Install `uv`

```bash
# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# Windows PowerShell — a clean Windows installation requires three one-time setup steps.
# CLOSE AND REOPEN POWERSHELL AFTER EACH STEP so that PATH changes take effect.
# (Skipping the reopen causes the next step to fail with "'git' / 'uv' is not recognized")

# (a) allow scripts to run (default policy is Restricted)
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser

# (b) install git (default Windows doesn't have it)
winget install --id Git.Git -e

# (c) install uv
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## 1. One-shot installer

Configures autostart on login, binds to localhost only. Takes approximately 3 minutes on a modern machine (10 or more on a slow VPS).

### 1a. One-line bootstrap (Linux + macOS + WSL2)

Installs `uv` if not already present, clones the repository into `~/muselab`,
then runs the platform installer end-to-end. Recommended for first-time installs:

```bash
curl -fsSL https://raw.githubusercontent.com/hesorchen/muselab/main/scripts/quick-install.sh | bash
```

To audit the script before piping it to the shell:

```bash
curl -fsSL https://raw.githubusercontent.com/hesorchen/muselab/main/scripts/quick-install.sh -o quick-install.sh
less quick-install.sh   # audit
bash quick-install.sh
```

### 1b. Manual install (step-by-step)

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

Script steps: pre-flight checks → `uv sync` → write `.env` with a random token →
7-question profile intake → register autostart → wait up to 30 seconds for the service to become available.

## 2. Open it

Local machine: `http://localhost:8765` → paste the token from `.env`.

### VPS

Do not expose the port directly to the internet. Use an SSH tunnel from your local machine:

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
token / provider keys) and gives specific guidance on any failure. Run it
when something appears to be wrong.

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

The container runs as a non-root `muse` user (uid 1000) with home directory
`/home/muse/.claude`. Bind-mount the host's `~/.claude` to that path to reuse
the OAuth credentials from `claude login`.

> **Host UID note.** The container's `muse` user is uid 1000. On most
> single-user Linux/macOS hosts the primary account is also uid 1000, so
> bind-mounts work without adjustment. If the host UID differs (multi-user
> host, custom macOS admin account, etc.), either run
> `chmod -R go+rX ~/.claude` and `chown -R 1000:1000 ~/muselab-archive`
> before starting the container, or pass `--user $(id -u):$(id -g)` and
> accept that the in-container `~/.claude` may be read-only.

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
