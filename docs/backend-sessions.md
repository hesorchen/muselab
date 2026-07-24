# Session internals

> [中文](backend-sessions_zh.md)

This page describes session ownership, storage, queues, attachments, forks, and restart recovery. See [Model routing and turn loop](routing.md) for streaming and reconnect behavior.

## Two-layer storage

Claude CLI and muselab jointly own each session:

```text
~/.claude/projects/<cwd-key>/<sid>.jsonl
    Claude-provider messages, tool calls, and compaction boundaries

${XDG_STATE_HOME:-~/.local/state}/muselab/vendor-cli/projects/<cwd-key>/<sid>.jsonl
    Isolated CLI transcripts for third-party providers

<repo>/sessions/
├── index.json
├── <sid>.sidecar.json
├── <sid>.queue.json
└── active_turns/<sid>.json

$MUSELAB_ROOT/.muselab-attach/<sid>/
    image and PDF originals persisted by muselab
```

- CLI JSONL is the source of truth for conversation content. Claude and
  third-party providers use different configuration roots. The third-party
  root is persistent user state and should be included in backups.
- `sessions/index.json` holds muselab-specific display and runtime settings.
- A sidecar holds per-message cost, model, time, attachment, and context annotations.
- A queue file holds pending messages.
- Attachments live under the primary `MUSELAB_ROOT`, even when the session belongs to another registered workspace.

Every layer uses the same session UUID.

## Multiple workspaces

Each index row stores a `cwd`. The Claude SDK starts in that directory, and its JSONL lives in the corresponding CLI project directory.

The session list scans every registered workspace and merges those results with the global index. Legacy rows without `cwd` belong to the primary `MUSELAB_ROOT`. Removing a workspace does not silently move its local session metadata to another root.

New interactive, forked, and scheduled sessions persist an explicit `cwd`. Existing model history is not automatically migrated across providers because thinking signatures may be incompatible.

## Session index

Primary fields in `sessions/index.json`:

| Field | Meaning |
|---|---|
| `id` | Session UUID, also the CLI JSONL filename |
| `name` | UI title |
| `model` | Locked model; empty means resolve a default on the first turn |
| `permission` | SDK permission mode |
| `cwd` | Owning workspace |
| `created_at`, `updated_at` | Unix seconds |
| `message_count`, `turn_count` | Message frames and real user turns |
| `pinned`, `auto_named` | Pin and automatic-title state |
| `effort`, `thinking` | Reasoning effort and extended-thinking switch |

Session listing supports pagination, search, forced inclusion of open tabs, and ETags so unchanged polls return `304`. Internal writes invalidate the cache immediately; external Claude CLI writes can appear after a short cache window.

## Sidecar

`<sid>.sidecar.json` does not duplicate conversation content. Its top-level data includes:

- `messages`: per-message annotations keyed by CLI message UUID.
- `context_max_tokens`: context capacity measured by the SDK.
- `pending_attachments`: attachment bundles waiting for a message UUID.

Common annotations include `cost`, `model`, `ts`, `elapsed_s`, `images`, and `docs`. Index and sidecar writes use separate process locks and temporary-file atomic replacement.

## Server-side message queue

An active turn does not discard later messages. They enter `<sid>.queue.json` and the backend drains them after the current turn, even when no browser remains connected.

```json
{
  "items": [
    {
      "id": "q-1234abcd",
      "text": "next message",
      "image_ids": "a1,b2",
      "permission": "default",
      "enqueued_at": 1718000000000
    }
  ],
  "paused": false
}
```

- Maximum depth is 10 per session.
- Order is FIFO by default and can be reordered or edited.
- The permission mode is captured when the item is queued.
- Errors, user questions, and explicit interruption pause the queue.
- A failed start race restores the item to the head.
- An empty, unpaused queue removes its file.
- `image_ids` reference staged attachments with a 10-minute TTL. An old queued item may lose its attachments, while its text still sends.

## Attachments

`POST /api/chat/upload-image` keeps its historical name but accepts:

- PNG, JPEG, GIF, and WebP;
- PDF;
- Markdown, text, CSV, JSON, YAML, source code, and related text formats;
- XLSX-family workbooks.

Before send, uploads live in memory for 10 minutes. The store is capped at 48 items or about 256 MiB; each file is capped at 10 MiB and inline text at 200 KiB. A backend restart loses staged but unsent uploads.

On send:

- Images become SDK image blocks and originals are saved under `$MUSELAB_ROOT/.muselab-attach/<sid>/<attachment-id>.<ext>`.
- PDFs become document blocks and also get a local copy so compatible providers can read them through tools.
- Text and workbooks are converted to bounded text and added to the prompt.
- Chat bubbles use small thumbnails; originals are served by `GET /api/chat/attachments/{sid}/{filename}?token=...`.

The SDK writes the user-message UUID later, so muselab first stores metadata in `pending_attachments` and binds it FIFO to the next matching user message. This queue is capped at 50 bundles and entries older than 24 hours are pruned.

## Forking and deletion

`POST /api/chat/sessions/{sid}/fork` copies either the full session or the transcript through `up_to_message_id`. The fork receives new session and message UUIDs, inherits the source model, workspace, permission, effort, and thinking settings, and records its source-session relationship. The tab menu forks the full conversation; the action below a completed turn forks through that turn. Historical-message editing remains a resend in the current session and does not fork. Switching models from a non-empty conversation creates a separate empty session rather than copying the transcript.

Deleting a session also removes:

- its CLI JSONL from the owning workspace;
- index, sidecar, queue, and active-turn sentinel data;
- transcript index and attachment directory;
- in-memory clients, replay registries, temporary spools, and background-task state.

## Restart recovery

Starting a turn writes `sessions/active_turns/<sid>.json`; normal completion removes it. If the process is killed, startup marks leftover rows as interrupted and lets the user decide whether to resend. muselab does not spend tokens by retrying automatically.

| State | Behavior after restart |
|---|---|
| Session list | Merged from the index and CLI JSONL across registered workspaces |
| Context capacity | Restored from the sidecar |
| Server queue | Restored from the queue file and drained by a later turn |
| Unsent attachments | Lost and must be uploaded again |
| Active turn | Offered as interrupted; not rerun automatically |

See [Data and backup](data-and-backup.md) for migration paths.
