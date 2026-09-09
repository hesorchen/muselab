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
# Linux / macOS / WSL2
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Windows via WSL2

On Windows, install through WSL2. One-time setup:

```powershell
# PowerShell (Administrator)
wsl --install            # installs WSL2 + Ubuntu default
# Reboot when prompted, then create your WSL Linux user
```

Current new Ubuntu WSL installations usually enable systemd. First check
whether the user manager is reachable:

```bash
systemctl --user show-environment >/dev/null
```

If unavailable, check the user session first. Only edit the configuration if
systemd is disabled; preserve the existing sections:

```bash
if [ -f /etc/wsl.conf ]; then
  sudo cp -p /etc/wsl.conf "/etc/wsl.conf.muselab-backup-$(date +%Y%m%dT%H%M%S)"
fi
sudoedit /etc/wsl.conf
```

Set `systemd=true` in the existing `[boot]` section, or add that section when
absent. Do not replace the whole file. After a change, run `wsl --shutdown`
from Windows PowerShell and reopen WSL. See [Microsoft's systemd guide](https://learn.microsoft.com/en-us/windows/wsl/systemd).


Reopen the WSL terminal and run the one-line install below.

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
# Linux / macOS / WSL2
git clone https://github.com/hesorchen/muselab && cd muselab

bash scripts/install-macos.sh    # macOS — user LaunchAgent
bash scripts/install-linux.sh    # Linux / WSL2 — user systemd service
```

Script steps: pre-flight checks → `uv sync` → select the primary workspace →
write `.env` with a random token and a stable absolute session metadata path →
register autostart → wait up to 30 seconds for the service to become available.
On an existing `.env`, the installer adds the session path only when missing
and never overwrites a custom value. It collects no personal profile, creates
no predefined directories, and does not write `CLAUDE.md` automatically.
Choose or configure a Provider in Settings after the first login.

## 2. Open it

Local machine: `http://localhost:8765` → paste the token from `.env`.

For durable project conventions, optionally run `bash scripts/intake.sh` to
create a generic workspace `CLAUDE.md`. It creates no fixed directory
structure. See [Configure workspace CLAUDE.md](personalize-claude-md.md).

### VPS

Do not expose the port directly to the internet. Use an SSH tunnel from your local machine:

```bash
ssh -L 8765:127.0.0.1:8765 your-vps-user@your-vps-host
# then visit http://localhost:8765 in your laptop's browser
```

Or use [Tailscale](https://tailscale.com) — same effect, no terminal.

### Deployment

muselab is designed for single-user self-hosting. The agent and terminal run with the service account's system permissions; workspaces do not provide OS-level isolation. Remote access should use an SSH tunnel or HTTPS with additional access control. See the [Security policy](../SECURITY.md) and [Security model](backend-security.md).

Working files and application state are stored on the deployment machine. When cloud models, embeddings, or remote vector stores are used, relevant data is sent to those services. See [Data & backup](data-and-backup.md) for backup and recovery procedures.

## 3. Verify

```bash
bash scripts/doctor.sh        # Linux / macOS / WSL2
```

`doctor` checks every layer (uv / claude CLI / `.env` / service / HTTP /
token / provider keys) and gives specific guidance on any failure. Run it
when something appears to be wrong.

### Your first task

After signing in and configuring a model, send a simple message and open a file to check its preview. Then select a project workspace and try a complete task:

> "Inspect the latest changes in this project, find why the tests became slower, fix the cause, run verification, and write the result to `docs/performance-note.md`."

This kind of task involves reading code and Git diffs, running targeted tests, editing files, and verifying changes. Expand tool records and code diffs to inspect the work. Open the generated note in the preview pane, or use a real terminal to check the result independently.

Code, research collections, and knowledge bases can all be workspaces; no fixed directory structure is required. See [Configure workspace CLAUDE.md](personalize-claude-md.md) for project context and [Workbench controls](workbench-ui.md) for file, session, and task operations.

## Auto-start after reboot?

| OS | Reboot → log back in | Reboot → never log in |
|----|---------------------|------------------------|
| **macOS** | ✅ auto-starts | n/a (always log in on Mac) |
| **Linux** | ✅ auto-starts | ⚠️ needs one-time `sudo loginctl enable-linger $USER` |
| **WSL2** | ✅ auto-starts (opening any WSL terminal triggers systemd-user) | ⚠️ after a Windows reboot, open a WSL terminal once — or configure [WSL boot autostart](https://learn.microsoft.com/en-us/windows/wsl/wsl-config) |

Per-OS detail (verify / restart / tail logs / expose to LAN / uninstall):
[macOS](install-macos.md) · [Linux](install-linux.md).

## Docker alternative

### Pre-built image from GHCR (multi-arch amd64 + arm64)

On Linux, check `id -u` / `id -g` before starting. If either differs from 1000, build the matching image described under mount permissions below first. The login token is saved in the private `~/.config/muselab/docker.env`; open it locally when signing in and do not share it.

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

The `/app/sessions` mount preserves indexes, annotations, queues, UI configuration under `config/`, and third-party transcripts under `state/muselab/vendor-cli/`. Saved UI values override matching bootstrap env-file values. Before replacing an older container, complete the [state migration](docker-state-migration.md).

> **Bind address.** The example above pins the port to `127.0.0.1` so the
> service is only reachable from the host. Plain `-p 8765:8765` binds
> `0.0.0.0` (all interfaces) — on a public VPS that leaves the portal
> reachable from the internet with only the token as a barrier. To expose
> it on a LAN (e.g. for phone access), use `-p 0.0.0.0:8765:8765` *and*
> put a firewall / reverse proxy in front. The bundled `docker-compose.yml`
> defaults to `127.0.0.1`; override with `MUSELAB_BIND=0.0.0.0` in `.env`.

The container runs as a non-root `muse` user (uid 1000) with home directory
`/home/muse/.claude`. Bind-mount the host's `~/.claude` to that path to reuse
the OAuth credentials from `claude login`.

**Bind-mount permissions.** Published images run as UID/GID 1000. Linux
host ownership must permit that user to read and write the workspace, sessions
and Claude state. Do not recursively grant other users access to `~/.claude`:
it contains OAuth credentials and transcripts. Token refresh and transcript
persistence both require write access; a read-only mount is not sufficient.

If the owner differs, use the source Compose build with a matching non-root
UID/GID. Create the host directories as your account before Docker mounts them:

```bash
mkdir -p data sessions
MUSELAB_UID=$(id -u) MUSELAB_GID=$(id -g) docker compose up -d --build
```

Set `MUSELAB_UID` and `MUSELAB_GID` in the Compose `.env` to retain that choice.
On Docker Desktop, verify the actual bind-mount permissions rather than assuming
the host account is UID 1000. A separate Claude state directory can isolate this
application's login and transcripts; it still needs private ownership and write
access. These options do not require changing your original credential files.

Pin a version: `ghcr.io/hesorchen/muselab:1.2.3` / `:1.2` / `:sha-abc1234`.

### Docker Compose

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
cp .env.example .env && $EDITOR .env    # set MUSELAB_TOKEN; ARCHIVE_DIR is the host workspace (legacy name)
claude login                              # host-side; container reuses OAuth
mkdir -p data sessions
docker compose up -d --build
```

### Native dev (uv, no service)

```bash
cd muselab && uv sync
cp .env.example .env && $EDITOR .env
claude login
uv run python -m backend.main             # binds MUSELAB_HOST:MUSELAB_PORT
```
