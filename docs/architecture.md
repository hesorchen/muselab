# Architecture

> [简体中文](architecture_zh.md)

muselab is a self-hosted workspace with no frontend build step. The browser owns file, preview, terminal, and session interaction; FastAPI owns authentication, persistent state, and real-time transport; every model call goes through the Claude Agent SDK.

```text
Browser
├── File tree and global search
├── File previews, editor, and real PTY terminals
├── Workspaces, sessions, queues, and activity center
└── Settings, providers, Skills, MCP, and scheduled tasks
        │
        ├── HTTP / ticket SSE
        └── ticket WebSocket
                │
FastAPI
├── Files / Workspaces / Preview
├── Chat / Sessions / Replay spool / Attachments
├── Terminals / Profiles / PTY workers
├── Scheduler / Push / Activity
└── Settings / Providers / MCP / Skills
                │
Claude Agent SDK → claude CLI
├── Claude OAuth
└── Anthropic-compatible providers
```

## Key design decisions

- **The SDK is the only model path.** Tool use, MCP, Skills, Subagents, plan mode, and `CLAUDE.md` come from the Claude Agent SDK. muselab does not create a parallel agent or system-prompt layer.
- **Native instruction ownership.** Persistent identity, response style, personal context, and durable rules belong in the SDK-discovered `CLAUDE.md` hierarchy. Reusable workflows belong in Skills. Tool behavior belongs in tool descriptions and permission enforcement.
- **Workspace binding.** `MUSELAB_ROOT` is the default workspace and additional local directories may be registered. Files, previews, terminals, and new-session cwd follow the active workspace; every session stores its own cwd.
- **Whole-file input.** The assistant reads complete files on demand through Read, Grep, Edit, and related tools. muselab does not pre-embed or chunk the archive.
- **Third-party provider isolation.** Each third-party provider receives per-request `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, and an isolated `CLAUDE_CONFIG_DIR`, preventing fallback to the wrong account.
- **No frontend build step.** The browser runs the HTML, JavaScript, and CSS directly. Browser dependencies are vendored under `frontend/vendor/`.
- **Short-lived tickets for real-time connections.** Chat obtains a one-time SSE ticket through an authenticated POST. Terminals obtain a one-time WebSocket ticket. Prompts and long-lived tokens do not enter real-time connection URLs.
- **Disconnects do not stop work.** Chat turns write to a disk replay spool so browsers can resume from a cursor. Terminals retain a bounded output buffer and permit reconnects after briefly leaving the surface.

## Runtime layout

```text
muselab/
├── backend/
│   ├── main.py                    # lifecycle and route mounting
│   ├── chat.py                    # agent turns, SSE, queues, background work
│   ├── sessions.py                # session index and sidecars
│   ├── files.py                   # file operations, preview data, trash
│   ├── workspaces.py              # workspace registration and selection
│   ├── terminal.py                # terminal API, profiles, connection manager
│   ├── terminal_worker.py         # PTY child process
│   ├── endpoints.py               # provider catalog and connection settings
│   ├── scheduler.py               # scheduled task runner
│   ├── push.py                    # Web Push and VAPID
│   ├── activity.py                # persisted activity-center state
│   ├── transcript_index.py        # transcript index
│   └── api_*.py                   # domain API routers
├── frontend/
│   ├── index.html
│   ├── app.js
│   ├── styles.css
│   ├── i18n/
│   └── vendor/
├── skills/                        # bundled Skills
├── scripts/                       # install, upgrade, diagnostics, templates
├── docs/                          # user and public technical docs
├── .claude/docs/                  # maintainer docs
├── .env                           # instance configuration and secrets
└── sessions/                      # session metadata, queues, attachments

$MUSELAB_ROOT/
├── CLAUDE.md                      # default-workspace instructions and context
├── user files
├── .muselab/
│   ├── workspaces.json
│   ├── scheduler.json
│   ├── activity.json
│   ├── terminal_profiles.json
│   ├── vapid.json
│   └── push_subs.json
└── .muselab-dustbin/

<another-registered-workspace>/
├── CLAUDE.md
├── user files
└── .muselab-dustbin/
```

Repository state, the default workspace, and additional workspaces are separate backup units. Conversation transcripts belong to the Claude CLI. Claude sessions normally live under its standard configuration directory, while third-party-provider sessions may live under isolated configuration roots. muselab's `sessions/` stores layered metadata such as name, cwd, model, cost, attachments, and queue.

## A chat turn

1. The browser calls `POST /api/chat/stream/start` with `X-Auth-Token`, sending the prompt, session, model, permission, effort, and attachment parameters.
2. The backend validates the session and workspace, then issues a short-lived, single-use ticket.
3. The browser opens SSE with that ticket. The old query form exists only as compatibility for older backends.
4. The backend obtains or creates an SDK client for the session, model, and reasoning settings, using the session-bound workspace as cwd.
5. The SDK loads `CLAUDE.md`, Skills, and MCP, then runs the tool and model loop.
6. Events are written to the replay spool and streamed to the browser. A reconnect can resume from its cursor.
7. The Claude CLI persists the transcript while muselab updates sidecars, usage, activity state, and notifications.

By default, the UI forks a non-empty session when switching to an incompatible model, avoiding cross-provider thinking-signature failures. The backend API still allows an explicit session-model update, so API callers own the compatibility risk.

## A terminal connection

1. The user creates a terminal in the active workspace and may choose a profile that runs a startup command.
2. The backend creates an independent PTY worker and passes only a safe environment allowlist to the terminal process.
3. The browser obtains a short-lived, single-use WebSocket ticket, then connects over a same-origin WebSocket.
4. The backend keeps a bounded in-memory output buffer for replay. Processes end after retention limits or service restart.

A terminal is a real shell running as the service user. It is not constrained by the Files API workspace boundary. Expose muselab only to trusted users.

## Subsystem documentation

| Page | Covers |
|---|---|
| [Model routing and chat loop](routing.md) | provider resolution, client lifecycle, replay, and SSE |
| [Session internals](backend-sessions.md) | index, sidecars, queue, attachments, fork, and recovery |
| [Files API](backend-files.md) | file endpoints, path boundaries, and trash |
| [Terminal](terminal.md) | PTY, profiles, connections, mobile behavior, and security |
| [Security model](backend-security.md) | authentication, permission surface, provider isolation, and limitations |
| [Frontend](frontend.md) | workspace state, rendering, performance, and PWA |
| [Skills](skills.md) | bundled Skills, discovery, and custom extensions |
| [Infrastructure](infrastructure.md) | installation, services, Docker, testing, and releases |
