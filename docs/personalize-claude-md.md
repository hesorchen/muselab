# Configure workspace CLAUDE.md

> [简体中文](personalize-claude-md_zh.md)

`CLAUDE.md` is an optional workspace-instruction file supported natively by the
Claude Agent SDK. Use it for project goals, sources of truth, edit boundaries,
run commands, and acceptance checks. muselab works normally without it.

The installer configures the primary workspace, login token, port, and model
access. It collects no personal profile and creates no predefined directory
structure.

## How it is loaded

- `MUSELAB_ROOT` selects the primary workspace.
- Additional local directories can be registered in the UI.
- Every workspace may have its own `CLAUDE.md`.
- A new session uses the active workspace as its `cwd`; the SDK loads
  instructions from that directory and its normal parent scopes.
- Changes apply on the next conversation without a service restart.

Keep durable, reusable conventions in `CLAUDE.md`. Put one-off requests in the
chat, and reusable procedures in a Skill.

## Optional generator

After installation, run this explicitly if the workspace needs instructions:

```bash
bash scripts/intake.sh
```

The helper reads `MUSELAB_ROOT` from `.env`, asks a few workspace questions,
and writes a generic `CLAUDE.md`. It writes only that file and does not create
or modify other directories. An existing file is backed up to `CLAUDE.md.bak`
after you confirm replacement.

The template language follows the shell locale by default:

```bash
MUSELAB_LOCALE=zh bash scripts/intake.sh
MUSELAB_LOCALE=en bash scripts/intake.sh
```

The generic templates are:

- `scripts/templates/default-CLAUDE.md`
- `scripts/templates/default-CLAUDE.en.md`

## Recommended content

A useful workspace guide usually needs only four sections:

```markdown
# CLAUDE.md

## Workspace purpose
- This is a Python service.
- Preserve API compatibility while reducing request latency.

## Sources and scope
- Treat docs/api.md and tests/ as sources of truth.
- backend/ and tests/ are safe to modify.
- Do not modify vendor/ or overwrite unrelated local changes.

## Run and verify
- Run locally: uv run python -m backend.main
- Verify before completion: uv run pytest tests/ -q
- Write artifacts to: docs/reports/

## Collaboration conventions
- Identify the cause before making the smallest useful change.
- Report changed scope, verification, and remaining risk.
```

Prefer concrete, executable guidance:

- Name real files and commands instead of vague aspirations.
- Separate sources of truth, editable scope, and protected content.
- Keep only rules that remain useful across many sessions.
- Never put passwords, tokens, private keys, or other secrets in `CLAUDE.md`.

## Multiple workspaces

Instructions belong to a directory, not to the muselab instance. A code
repository, research collection, and operations dataset can be registered as
separate workspaces with different `CLAUDE.md` files. Switching workspace moves
the file tree, preview, terminals, and new-session `cwd` together.

The primary workspace also stores global state under `.muselab/`, so do not
remove or move it casually even when most daily work happens elsewhere. See
[Configuration](configuration.md) and [Data and backup](data-and-backup.md).

## Security boundary

`CLAUDE.md` provides instructions; it is not a permission sandbox. The Files
API is contained to registered workspaces, but real terminals run with the
muselab service user's OS authority and may reach paths outside a workspace.
Register only directories you intend to expose, and do not publish the service
directly to an untrusted network.
