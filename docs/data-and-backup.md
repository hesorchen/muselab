# Data and backup

> [中文](data-and-backup_zh.md)

muselab has no database, but its state does not live in one directory. A complete migration covers workspaces, repository state, Claude CLI data, and optional browser-local preferences.

## Primary workspace

Back up the complete `$MUSELAB_ROOT/` when practical. In addition to user files, it contains:

| Path | Content | Recommendation |
|---|---|---|
| `.muselab/workspaces.json` | Registered workspaces and order | Required; absolute paths may need updating after migration |
| `.muselab/scheduler.json` | Scheduled tasks, history, and unread count | Required when using the scheduler |
| `.muselab/activity.json` | Cross-workspace Activity Center state | Recommended |
| `.muselab/terminal_profiles.json` | Terminal profiles, startup commands, and default selection | Required when using profiles; commands may be sensitive |
| `.muselab/vapid.json` | Web Push VAPID private/public keypair | Recommended together with subscriptions |
| `.muselab/push_subs.json` | Device Push subscriptions | Back up with `vapid.json` to preserve subscriptions |
| `.muselab/imagegen/` | Image-generation job history and durable files | Back up to preserve image history |
| `.muselab-attach/` | Conversation image and PDF originals | Required to preserve attachment previews |
| `.muselab-dustbin/` | Recoverable dustbin for the primary workspace | Back up to preserve recovery |

Deleting `vapid.json` creates a new keypair and invalidates existing browser subscriptions. Restoring `push_subs.json` without its matching VAPID key is not useful.

## Additional workspaces

Back up each registered workspace as needed:

- its user files;
- its own `.muselab-dustbin/`;
- its CLI JSONL outside the workspace: Claude under `~/.claude/projects/`,
  third-party providers under the isolated temporary configuration root.

Global state remains only under the primary `MUSELAB_ROOT/.muselab/`; it is not copied into every workspace.

## Repository state

| Path | Content |
|---|---|
| `<repo>/.env` | Token, provider keys, and deployment configuration; contains secrets |
| `<repo>/sessions/` | Session index, sidecars, queues, active-turn sentinels, and derived indexes |
| `<repo>/mcp.json` | MCP server configuration, possibly with credentials |
| `<repo>/provider_overrides.json` | Built-in provider edits and custom providers |

Source code, `.venv/`, dependency caches, build output, and logs can be restored from the repository or installer and do not need to be treated as user data.

## Claude CLI data

| Path | Content |
|---|---|
| `~/.claude/projects/<cwd-key>/*.jsonl` | Real conversation transcripts for each workspace |
| `~/.claude/.credentials.json` | Claude Pro/Max OAuth login |
| Other files under `~/.claude/` | User-level CLAUDE.md, Skills, permissions, and CLI preferences |
| `<system-temp>/muselab-vendor-cli-config-<uid>/projects/` | Isolated third-party-provider transcripts; the OS may clean this directory |

If you only use Claude, the simplest safe approach is to back up all of
`~/.claude/`. If you use third-party providers, also back up the isolated
`projects/` directory or those transcripts will not be present in the
`~/.claude/` backup. If credentials are not migrated, run `claude login` again
on the new machine.

## Ephemeral or unnecessary state

| State | Reason |
|---|---|
| Running and exited terminal sessions | Process-local; only profiles are durable |
| SSE replay spools | OS temporary files used only for same-process reconnect |
| Staged, unsent attachments | Memory-only with a 10-minute TTL |
| Isolated third-party-provider configuration except `projects/` | Recreated automatically; the `projects/` transcripts must be backed up |
| SDK clients, rate-limit buckets, and memory caches | Rebuilt after startup |
| Open tabs, layout, and some UI preferences | Browser localStorage; migrate browser data separately if needed |

## Restore procedure

1. Install the same or a newer muselab version on the new machine.
2. Stop the service.
3. Restore the primary workspace, required additional workspaces, repository state, `~/.claude/`, and isolated transcripts when third-party providers are used.
4. Check `MUSELAB_ROOT` in `.env` and update stale absolute paths through the workspace picker.
5. Verify ownership and permissions, especially for `.env`, Claude credentials, VAPID keys, and terminal profiles.
6. Start the service and test workspaces, session history, attachments, scheduled tasks, terminal profiles, image history, and Push.
7. Run `bash scripts/doctor.sh` for a basic health check.

Backups contain tokens, API keys, OAuth credentials, and Push private keys. Terminal profiles can also contain user-written commands. Encrypt them and never commit them to Git or place them on a shared drive.
