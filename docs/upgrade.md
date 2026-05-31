# Upgrading

> [简体中文](upgrade_zh.md)

muselab tracks fast-moving upstream pieces — the Claude Agent SDK and the
`claude` CLI. `scripts/upgrade.sh` bumps both, smoke-tests the result, and
leaves your data untouched.

## Steps

```bash
cd ~/muselab            # your repo
git pull                # get the latest muselab code
bash scripts/upgrade.sh
```

What it does:

1. Upgrades the Python `claude-agent-sdk` (`uv lock --upgrade-package …` then
   `uv sync --frozen`).
2. Upgrades the `claude` CLI (`npm install -g @anthropic-ai/claude-code@latest`).
3. Runs the test suite (`uv run pytest tests/ -q`) as a smoke test.

If the tests **fail**, the script aborts and rolls the Python deps back
(`git checkout uv.lock pyproject.toml && uv sync`) — most often this means a new
SDK release changed an API muselab relies on. Check the printed log and open an
issue.

## After upgrading

The script does **not** restart the service or commit the lockfile changes —
it prints the exact commands. Restart so the new SDK/CLI take effect:

```bash
# Linux (systemd --user)
systemctl --user restart muselab

# macOS (launchd)
launchctl kickstart -k gui/$UID/com.muselab
```

Then review and commit the dependency bump if you keep your repo under git:

```bash
git diff uv.lock
git add uv.lock pyproject.toml && git commit -m "chore: bump claude-agent-sdk"
```

## What's preserved

Upgrades never touch `.env`, `sessions/`, or your archive. The pinned `claude`
CLI version lives in `scripts/versions.env` (and is mirrored in the Dockerfile);
`upgrade.sh` moves you to the latest. There is no schema migration step — the
JSON state files are forward-tolerant, and the few migrations that exist (e.g.
the VAPID key format) run automatically at backend startup.

## Docker

Pull the new image and recreate the container:

```bash
docker compose pull && docker compose up -d
```

Your archive and `.env` are bind-mounted, so they survive the recreate.
