# Infrastructure

> [简体中文](infrastructure_zh.md)

This page maps every operational layer of muselab — the installer scripts, service units, Docker image, dev server, test suite, and CI/CD pipelines. It is aimed at contributors and operators who need to understand **what exists and how it fits together**. For step-by-step how-to instructions see [Quick start](quickstart.md), [install-linux](install-linux.md), [install-macos](install-macos.md), [Upgrading](upgrade.md), and [CONTRIBUTING.md](../CONTRIBUTING.md).

---

## 1. The scripts/ toolbox

All automation lives in `scripts/`. Each script is standalone bash and runs from the repo root.

| Script | Purpose | Key env flags |
|--------|---------|---------------|
| `versions.env` | Single source of truth for pinned external tool versions. Both platform installers source it; the Dockerfile mirrors the Claude CLI pin. | — |
| `quick-install.sh` | One-line bootstrap (`curl … \| bash`). Refuses root, detects OS, installs `uv` if missing, and hands off to the platform installer with interactive input preserved. | `MUSELAB_NONINTERACTIVE=1` |
| `install-linux.sh` | Full Linux/WSL2 installer: prerequisites → dependencies → `.env` → systemd user unit → linger check. Includes the `CLAUDE.md` profile intake. | `MUSELAB_NONINTERACTIVE=1`, `MUSELAB_LOCALE=zh\|en`, `MUSELAB_SKIP_SERVICE=1`, `MUSELAB_NO_BROWSER=1` |
| `install-macos.sh` | macOS equivalent that registers a launchd LaunchAgent. | same four flags |
| `uninstall-linux.sh` | Stops and removes the systemd unit; leaves user data untouched. | — |
| `uninstall-macos.sh` | Unloads and removes the LaunchAgent; leaves user data untouched. | — |
| `upgrade.sh` | Upgrades the SDK and CLI, runs a smoke test, and prints rollback guidance on failure. It does not commit or restart the service. See [Upgrading](upgrade.md). | — |
| `doctor.sh` | Checks prerequisites, configuration, dependencies, service status, HTTP authentication, and provider credentials. | — |
| `setup-https.sh` | Linux-only Caddy reverse-proxy setup with SSE-safe flushing, HSTS, and firewall rules. | — |
| `intake.sh` | Re-runs the `CLAUDE.md` profile setup and backs up the existing file before overwriting. | — |
| `lint.sh` | Runs repository consistency and privacy-leak checks. | `MUSELAB_LEAK_BLACKLIST` |

---

## 2. Service management

### Linux — systemd user unit

Unit file: `~/.config/systemd/user/muselab.service` (generated from `scripts/templates/muselab.service.tmpl`).

Resource ceilings: `MemoryHigh=2G`, `MemoryMax=4G`, `LimitNOFILE=8192`, `TasksMax=4096`. Restart policy: `on-failure`, `RestartSec=10`, max 5 restarts per 5 minutes.

```bash
systemctl --user restart muselab
systemctl --user reset-failed          # clear crash counter after 5 restarts
journalctl --user -u muselab -f
sudo loginctl enable-linger $USER      # survive logout/reboot on a VPS
```

**VPS caveat:** without `loginctl enable-linger`, the user unit stops when the SSH session ends. The installer warns if linger is not yet active.

### macOS — launchd LaunchAgent

Plist: `~/Library/LaunchAgents/com.muselab.plist` (generated from `scripts/templates/com.muselab.plist.tmpl`; label `com.muselab`). `KeepAlive` on crash or non-zero exit; `ThrottleInterval=10s`; `HardResourceLimits`: 8192 fd, 4096 processes. Logs: `~/Library/Logs/muselab/stdout.log` and `stderr.log`.

```bash
launchctl kickstart -k gui/$UID/com.muselab    # restart
tail -f ~/Library/Logs/muselab/stderr.log
```

> macOS memory limits are advisory (jetsam, not cgroup). The 4 GiB `MemoryMax` hard kill is Linux-only.

---

## 3. Docker

### Two-stage build

The `Dockerfile` uses two stages to keep the final image slim:

**Stage 1 — builder:** `python:3.12-slim` base; copies pinned `uv`/`uvx`; installs production Python dependencies with `uv sync --frozen --no-dev --no-install-project`.

**Stage 2 — runtime:** fresh `python:3.12-slim`; installs `curl`, `git`, Node 20, and a pinned Claude Code CLI; copies the pre-built `.venv` plus `backend/`, `frontend/`, `skills/`, and `scripts/templates/`; creates a non-root `muse` user; exposes port 8765; and declares an `/api/health` health check.

### docker-compose.yml

`docker-compose.yml` runs a single service with these defaults:

| Compose setting | Default | Override |
|-----------------|---------|----------|
| Port bind | `127.0.0.1:8765:8765` | `MUSELAB_BIND`, `MUSELAB_PORT` |
| Archive volume | `./data:/data` | `ARCHIVE_DIR` |
| Claude credentials | `~/.claude:/home/muse/.claude` | `CLAUDE_HOME` |
| Sessions volume | `./sessions:/app/sessions` | — |
| Memory limit | `4g` hard / `1g` reservation | — |
| `pids_limit` | `4096` | — |
| Restart policy | `unless-stopped` | — |

The `~/.claude` mount is read-write by design: the Claude CLI needs write access to refresh OAuth tokens and persist session history. The compose file forces `ANTHROPIC_API_KEY=""` and `ANTHROPIC_AUTH_TOKEN=""` to ensure the SDK uses OAuth rather than a console API key.

### GHCR multi-arch image

Image: `ghcr.io/hesorchen/muselab`

| Tag pattern | Published when |
|-------------|----------------|
| `latest` | Every commit to `main` |
| `{version}`, `{major}.{minor}`, `{major}` | Git tag `v*.*.*` |
| `sha-{short}` | Every commit to `main` |

Architectures: `linux/amd64` and `linux/arm64` via QEMU.

---

## 4. Dev mode

```bash
# One-time setup
git clone https://github.com/hesorchen/muselab && cd muselab
uv sync
cp .env.example .env    # set MUSELAB_TOKEN and MUSELAB_ROOT

# Run the dev server (hot-reload, no build step needed)
make run
# equivalent: uv run uvicorn backend.main:app --host 0.0.0.0 --port 8765 --reload
```

The frontend is plain HTML + Alpine.js v3 (vendored). There is no separate frontend dev server and no `npm install` — edit `frontend/*.html|js|css` and hard-refresh the browser. See [CONTRIBUTING.md](../CONTRIBUTING.md) for the full contributor workflow.

### Makefile targets

| Target | Command | Notes |
|--------|---------|-------|
| `make run` | `uv run uvicorn … --reload` | Dev server with hot-reload |
| `make test` | `uv run pytest -v` | All tests, verbose |
| `make test-fast` | `uv run pytest -x --tb=short` | Stop on first failure |
| `make lint` | `uv run python -m compileall -q backend tests` | Syntax check only; CI uses `ruff check` |

---

## 5. Test suite

**Framework:** pytest ≥ 9.1.1 with pytest-asyncio ≥ 1.4.0.

**Layout:** `tests/` contains unit, integration, and Playwright browser tests. File and line counts change frequently and are not treated as stable project metrics. E2E tests live in `tests/e2e/` and are gated by `RUN_E2E=1`.

### Isolation strategy

The shared `app_module` fixture:
- monkeypatches `MUSELAB_TOKEN`, `MUSELAB_ROOT`, `MUSELAB_PORT=9999`
- redirects `MUSELAB_ENV_PATH` to a throwaway temp file so tests never touch the real `.env`
- purges all provider API key env vars
- deletes all `backend.*` entries from `sys.modules` to force a full re-import
- isolates `sessions/` to a temp directory

The `temp_root` fixture creates a throwaway archive tree with a `notes/` subtree, a `.secret` file, and a `.env` file specifically for path-traversal security tests.

### Notable test files

| File | What it covers |
|------|----------------|
| `test_chat_stream.py` | SSE streaming, tool-call events, cancellation |
| `test_regressions.py` | Cross-subsystem bug regression suite |
| `test_scheduler.py` | Scheduled task runner |
| `test_files.py` | File browser, upload, path-traversal security |
| `test_sessions.py` | Session CRUD and index |
| `test_security.py` | Auth bypass, token validation |

### E2E (Playwright)

`tests/e2e/` uses Playwright + Chromium and is not included in the default `pytest tests/` run. Set `RUN_E2E=1` and install Chromium separately. Current coverage includes multi-tab lifecycle, file previews, chat rendering performance, and mobile terminal interactions.

---

## 6. CI/CD

### ci.yml

Triggers: push to `main`, version tags `v*.*.*`, PRs to `main`.

| Job | Runner(s) | Blocking? | What it does |
|-----|-----------|-----------|-------------|
| `test` | ubuntu-latest (py 3.12 + 3.13), macos-latest (py 3.12) | yes | `uv sync --frozen` → `pytest tests/ -v`; coverage report on Linux py 3.12 (non-blocking, ephemeral `pytest-cov`) |
| `lint` | ubuntu-latest | yes | `ruff check backend/ tests/` + `bash scripts/lint.sh` |
| `frontend-lint` | ubuntu-latest (Node 20) | yes | `node --check` on `app.js`, `sw.js`, `constants.js`, `i18n/index.js`; JSON validation of `manifest.webmanifest` |
| `security` | ubuntu-latest | no | `pip-audit` against the frozen lockfile |
| `e2e` | ubuntu-latest | no | Playwright/Chromium, 2 retries via `pytest-rerunfailures` |
| `docker` | ubuntu-latest | yes (push jobs) | PRs: single-arch build, no push. main/tags: multi-arch build + push to `ghcr.io` |

CI test env vars: `MUSELAB_TOKEN=ci-test-token-1234567890abcdef-min-32`, `MUSELAB_ROOT=${{ github.workspace }}/.ci-archive`.

### install-test.yml

Path-filtered to installer scripts, `pyproject.toml`, `uv.lock`, `Dockerfile`, `docker-compose.yml`. Runs real installer end-to-end on four OS images:

| Job | Runner(s) | Notes |
|-----|-----------|-------|
| `linux` | ubuntu-22.04, ubuntu-24.04 | `MUSELAB_NONINTERACTIVE=1 MUSELAB_SKIP_SERVICE=1 MUSELAB_NO_BROWSER=1`; polls `/api/health` 30 s |
| `macos` | macos-13 (Intel, `continue-on-error`), macos-14 (ARM, required) | 20-minute job timeout |
| `docker-run` | ubuntu-latest | Builds locally, runs container, polls `/api/health`, confirms Docker `HEALTHCHECK` reaches `healthy` within 90 s |

`.env` is explicitly excluded from failure artifact uploads to prevent leaking `MUSELAB_TOKEN`.

### Release

Push a git tag matching `v*.*.*`. The `docker` job in `ci.yml` automatically publishes multi-arch images with the full semver tag matrix to `ghcr.io/hesorchen/muselab`. Changelog and GitHub Release creation are handled manually.

### Dependabot

Weekly uv bumps (grouped: `claude-agent-sdk`/`anthropic*` in one PR; `fastapi`/`uvicorn`/`starlette`/`pydantic` in another; max 5 open PRs). Monthly GitHub Actions bumps.

---

## 7. Packaging

**File:** `pyproject.toml` — `requires-python = ">=3.12"`, MIT license.

### Key dependency decisions

| Package | Constraint | Rationale |
|---------|-----------|-----------|
| `claude-agent-sdk` | `>=0.2.120,<0.3` | The upper bound is deliberate: muselab relies on assumptions about the SDK tool denylist and JSONL transcript format, so minor-version upgrades require explicit validation. |
| `starlette` | `>=1.3.1` | Explicitly constrained to a security-fixed release. |
| `pyjwt[crypto]` | `>=2.13.0` | Pinned above the mcp-transitive 2.12.1 (PYSEC-2026-175/177/178/179). |

### uv usage

| Command | Where used |
|---------|-----------|
| `uv sync --frozen` | All install scripts, CI, Docker build — ensures exact reproducibility from `uv.lock` |
| `uv lock --upgrade-package claude-agent-sdk` | `scripts/upgrade.sh` — selective bump without touching other deps |
| `uv run --with <pkg>` | CI ephemeral tools (`pytest-cov`, `pip-audit`) without modifying the frozen lockfile |
| `uv run uvicorn …` | Dev server and systemd `ExecStart` |

The `uv` binary is pinned in the Dockerfile so image rebuilds remain reproducible; see `Dockerfile` for the exact current version.
