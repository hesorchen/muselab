# Architecture

> [简体中文](architecture_zh.md)

```mermaid
flowchart TB
  subgraph Browser["Browser · ~27k LOC vanilla HTML + Alpine.js + CSS"]
    F[📁 files] --- P[📄 preview + tabs] --- C[💬 chat + multi-model]
  end
  Browser ==>|HTTP / SSE| BE
  subgraph BE["Backend · FastAPI ~14k LOC"]
    A["/api/files/*<br/>safe-resolve · read/write/grep"]
    B["/api/chat/*<br/>ClaudeSDKClient pool<br/>per (session, model, effort)"]
  end
  BE ==> SDK[Claude Agent SDK<br/>spawns claude CLI subprocess]
  SDK -->|claude-* models<br/>via Pro OAuth| AN[api.anthropic.com]
  SDK -->|env override per request| V[Vendor Anthropic-compat endpoints]
  V --> DS[DeepSeek]
  V --> GL[智谱 GLM]
  V --> MM[MiniMax]
  V --> KM[Kimi]
  V --> QW[Qwen]
  V --> XM[Xiaomi MiMo]
  V --> QF[Baidu Qianfan (ERNIE)]
  V --> CG[Codex Gateway]
```

## Key design decisions

- **SDK over raw API.** Claude Agent SDK (same engine as Claude Code), so MCP / Skills / Subagents / plan mode / `CLAUDE.md` auto-load behave uniformly across providers. New providers: see [add-provider.md](add-provider.md).

- **Per-session `env=` override.** Third-party providers are wired by setting `ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY` + an isolated `CLAUDE_CONFIG_DIR` ([`backend/endpoints.py:L851`](../backend/endpoints.py#L851)). The last one blocks the CLI from silently falling back to Pro OAuth and routing third-party traffic through your Anthropic account — the full mechanism is in [Model routing § env injection](routing.md#3-third-party-env-injection).

- **No build step.** Edit `frontend/`, refresh the browser. Vetted third-party libs live in `vendor/` (licenses in [THIRD_PARTY_LICENSES.md](../THIRD_PARTY_LICENSES.md)); installation never touches npm.

- **Client cache keyed by `(session_id, model, effort)`** ([`backend/chat.py:L303`](../backend/chat.py#L303)). Switching model or reasoning effort lands on its own pooled client; each assistant message stores its own `model` field so badges stay accurate after reload. Pool cap, LRU rules: [Model routing § client pool](routing.md#2-the-client-pool).

- **Whole-file as the unit of input.** `MUSELAB_ROOT` is a directory you own; the root-level `CLAUDE.md` auto-loads on every conversation. The assistant reaches files via Read / Grep / Edit on demand — no pre-embedding.

## Directory map

Two roots matter at runtime: the **repo** (code + per-install state) and the
**archive** (`MUSELAB_ROOT`, your own files). They are deliberately separate so
you can back up or move your data without touching the install.

```
muselab/                      # repo root
├── backend/                  # FastAPI app (~14k LOC)
│   ├── main.py               # app factory, uvicorn entry, route mounting
│   ├── auth.py               # X-Auth-Token guard (header or ?token=)
│   ├── chat.py               # /api/chat/* — SDK client pool, SSE turn loop
│   ├── endpoints.py          # provider catalog + per-request env wiring
│   ├── files.py              # /api/files/* — safe-resolve read/write/grep
│   ├── sessions.py           # session index + sidecar + queue (repo/sessions/)
│   ├── scheduler.py          # asyncio cron loop → <archive>/.muselab/scheduler.json
│   ├── push.py               # Web Push / VAPID → <archive>/.muselab/
│   ├── api_settings.py       # /api/settings — hot-rewrites .env + os.environ
│   └── prompts.py            # system-prompt assembly
├── frontend/                 # vanilla HTML + Alpine.js + CSS (~27k LOC, no build)
│   ├── index.html  app.js  styles.css
│   ├── i18n/                 # EN/ZH UI strings
│   └── vendor/               # vetted third-party libs (see THIRD_PARTY_LICENSES.md)
├── scripts/                  # install / upgrade / uninstall / doctor / https
├── skills/                   # bundled Claude skills
├── docs/                     # this folder
├── .env                      # ← per-install config + secrets (gitignored)
└── sessions/                 # ← session metadata, sidecars, queues (gitignored)

$MUSELAB_ROOT/                # the archive — YOUR files, never inside the repo
├── CLAUDE.md                 # auto-loads every conversation
├── health/ work/ money/ …    # whatever subdirs you create
└── .muselab/                 # scheduler.json · vapid.json · push_subs.json
```

The actual conversation transcripts are owned by the Claude CLI, not muselab:
they live under `~/.claude/projects/<cwd-key>/<session-id>.jsonl`. muselab's
`sessions/` only holds the metadata layered on top (names, per-message model
badge, cost, uploaded attachments). See
[Data & backup](data-and-backup.md) for the full back-up list.

## A request, end to end

A chat turn is one Server-Sent Events (SSE) stream:

1. **Browser → backend.** `GET /api/chat/stream` with the prompt, session id
   and chosen `model` as query parameters
   ([`backend/chat.py:L5043`](../backend/chat.py#L5043)). The auth token rides
   as `?token=` because `EventSource` cannot set headers
   ([Security model § authentication](backend-security.md#authentication)).
2. **Model resolution & lock.** The session is locked to one model
   (`sessions.py`). The first turn pins it; later turns reuse it so a
   conversation never mixes vendors mid-stream (cross-vendor *thinking
   signatures* don't transfer). A session created before any provider existed
   self-heals to a configured model on its first real send.
3. **Client pool.** `chat.py` fetches or spawns a `ClaudeSDKClient` keyed by
   `(session_id, model, effort)`. For a third-party model it sets
   `ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY` + an **isolated**
   `CLAUDE_CONFIG_DIR` so the CLI can't fall back to your Pro OAuth and bill the
   wrong account.
4. **Agent loop.** The SDK spawns the `claude` CLI subprocess, which runs the
   full loop — tool calls (Read/Grep/Edit/Bash), MCP servers, skills, plan mode
   — against your archive as the working directory.
5. **Backend → browser (SSE).** Tokens, tool-call events, and a final `done`
   event stream back. The turn is published through a `TurnBroadcast`, so a
   browser disconnect never kills the reply — reconnecting replays the buffer
   ([Model routing § SSE turn loop](routing.md#4-the-sse-turn-loop)). The
   frontend renders incrementally; each assistant message records its own
   `model` so badges stay correct after reload.
6. **Persistence.** The CLI appends the transcript to its JSONL; muselab writes
   the sidecar (cost, model, attachments). If the turn was long and Web Push is
   configured, a completion notification fires even with the tab closed.

Scheduled runs ([scheduler.md](scheduler.md)) take the same path from step 3,
minus a human — they run unattended with the full permission set.

## Going deeper

This page is the map. Each subsystem has its own page with source-linked
detail:

| Page | Covers |
|---|---|
| [Model routing & chat loop](routing.md) | model resolution, client pool, env injection, every SSE event type |
| [Session internals](backend-sessions.md) | index, sidecars, message queue, attachments, fork, restart recovery |
| [Files API](backend-files.md) | all `/api/files/*` endpoints, `safe_resolve`, trash |
| [Security model](backend-security.md) | auth, settings surface, billing isolation, known limitations |
| [Frontend internals](frontend.md) | no-build SPA, rendering pipeline, SSE client, i18n, service worker |
| [Skills](skills.md) | bundled skills, discovery, adding your own |
| [Infrastructure](infrastructure.md) | scripts, systemd/launchd, Docker, tests, CI/CD |
| [Glossary](glossary.md) | every muselab term of art, defined once |
