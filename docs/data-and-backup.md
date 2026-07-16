# Data & backup

> [简体中文](data-and-backup_zh.md)

muselab keeps no database — all state is plain files in three places:

1. the **workspaces** (`MUSELAB_ROOT` plus registered directories) — your own files,
2. the **repo** — config and session metadata,
3. **`~/.claude/`** — the Claude CLI's transcripts and login.

To migrate a muselab install to a new machine, copy the three "must back up"
sets below. Everything else regenerates on its own.

## What to back up

| Path | Contains | Why it matters |
|---|---|---|
| `$MUSELAB_ROOT/` | Your archive — every file you put there | This *is* your data |
| Every additional registered workspace | Its files and `.muselab-dustbin/` | Required to restore that workspace and its recoverable trash |
| `$MUSELAB_ROOT/.muselab/workspaces.json` | Registered workspace paths and labels | Restores the picker; paths must still exist after migration |
| `$MUSELAB_ROOT/.muselab/scheduler.json` | Scheduled tasks + run history | Lose it = recreate every schedule |
| `<repo>/.env` | All config **including secrets** (token + provider keys) | Holds credentials — back up securely, never commit |
| `<repo>/sessions/` | Session index, per-message sidecars (cost, model badge, uploaded attachments), pending queues | muselab-only metadata, not in the CLI transcript |
| `<repo>/mcp.json` | MCP server configuration | Only if you configured MCP |
| `<repo>/provider_overrides.json` | Edits to built-in providers + any custom providers | Only if you customized providers |
| `~/.claude/projects/<cwd-key>/*.jsonl` | **The actual conversation transcripts** | The real chat history — owned by the CLI |
| `~/.claude/.credentials.json` | Claude Pro/Max OAuth login | Skip and you just re-run `claude login` |

> `.env` and `~/.claude/.credentials.json` contain secrets. Back them up to a
> private location; do not put them in a git repo or shared drive.

## What you don't need to back up

These are regenerated automatically:

| Path | Note |
|---|---|
| `$MUSELAB_ROOT/.muselab/vapid.json` | Web-push keypair — regenerates, but deleting it forces every device to re-subscribe |
| `$MUSELAB_ROOT/.muselab/push_subs.json` | Push subscriptions — devices re-subscribe on their own |
| `$MUSELAB_ROOT/.muselab-dustbin/` | Soft-delete trash, auto-purged after `MUSELAB_TRASH_TTL_DAYS` |
| `/tmp/muselab-vendor-cli-config-*` | Ephemeral isolated CLI config for third-party providers |
| `<repo>/.venv/`, caches, logs | Rebuilt by `uv sync` / at runtime |

## Restore on a new machine

1. Install muselab normally (see [Quick start](quickstart.md)).
2. Stop the service.
3. Restore `$MUSELAB_ROOT/` (including its `.muselab/scheduler.json` and
   `.muselab/workspaces.json`), every additional workspace you need,
   the repo's `.env` / `sessions/` / `mcp.json` / `provider_overrides.json`,
   and `~/.claude/`.
4. Make sure `MUSELAB_ROOT` in the restored `.env` points at the default
   workspace's new location, then update stale paths in the workspace picker.
5. Start the service. Switch through the restored workspaces and verify one
   transcript, one file preview, schedules, and history.

A quick health check after restore: `bash scripts/doctor.sh`.
