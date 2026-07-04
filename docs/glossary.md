# Glossary

> [简体中文](glossary_zh.md)

Terms of art used across the muselab codebase and docs, defined once and linked from elsewhere.

---

**active-turn sidecar** — A small JSON file written to `sessions/active_turns/<sid>.json` at the start of each turn and deleted on clean completion. If muselab is killed mid-turn the file survives and surfaces a "unfinished turn" toast on the next browser session. See [`backend-sessions.md — Active-turn sidecars`](backend-sessions.md#active-turn-sidecars).

**active turn** — A chat turn that is currently streaming. muselab tracks active turns in `_active_turns[sid]`; a session can have at most one active turn at a time. An attempt to start a second turn while one is running raises `_TurnBusy`. See [`routing.md — The SSE Turn Loop`](routing.md#4-the-sse-turn-loop).

**archive root** — The directory pointed to by `MUSELAB_ROOT`. This is where your own files live: `CLAUDE.md`, subdirectories, attachments (`.muselab-attach/`), scheduler state (`.muselab/`), and the trash (`.muselab-dustbin/`). It is deliberately outside the repo root so data and code can be backed up or moved independently. See [`architecture.md`](architecture.md#directory-map).

**cost capture** — After each turn, the `ResultMessage.total_cost_usd` value is added to a process-wide aggregate and a per-session accumulator, and written to the sidecar as a per-message annotation. Cost is only populated for Claude (Anthropic) models; third-party vendors return 0. See [`routing.md`](routing.md) and [`backend/chat.py:L6176`](../backend/chat.py#L6176).

**cwd-key** — The hashed directory path used by the Claude CLI to namespace JSONL files under `~/.claude/projects/`. For a given archive root the key looks like `-home-alice-archive`. muselab uses the same derivation so its session IDs map 1-to-1 to CLI filenames. See [`backend-sessions.md — Two-layer store`](backend-sessions.md#1-two-layer-store--ownership).

**CLAUDE.md** — A plain-text Markdown file at the root of the archive. The Claude Agent SDK auto-loads it as context on every conversation, making it the primary channel for personalising Muse's behaviour. See [`personalize-claude-md.md`](personalize-claude-md.md).

**CLAUDE_CONFIG_DIR isolation** — For third-party providers, muselab sets `CLAUDE_CONFIG_DIR` to a per-user temp directory (`$(tmpdir)/muselab-vendor-cli-config-<uid>/`) that contains no `credentials.json`. This prevents the CLI from silently falling back to Claude Pro OAuth and routing third-party traffic through your Anthropic account. See [`routing.md — CLAUDE_CONFIG_DIR isolation`](routing.md#why-claude_config_dir-isolation-prevents-oauth-fallback-billing) and [`backend/endpoints.py:L879`](../backend/endpoints.py#L879).

**client pool** — The in-memory cache of live `ClaudeSDKClient` instances, keyed by `(session_id, model, effort)`. Default capacity is 3 entries (configurable via `MUSELAB_CLIENT_POOL_CAP`); least-recently-used entries are evicted when the cap is exceeded, except for entries with an active turn or in-flight background task. See [`routing.md — The Client Pool`](routing.md#2-the-client-pool) and [`backend/chat.py:L302`](../backend/chat.py#L302).

**effort** — The reasoning effort level passed to `ClaudeAgentOptions`. Valid values are `"low"`, `"medium"`, `"high"`, `"xhigh"`, `"max"`, or `""` (SDK adaptive default). Stored per-session in `sessions/index.json`; changing it disconnects the cached client so the next turn rebuilds with the new value. Effort is part of the client pool cache key. See [`routing.md — Reasoning Effort and Extended Thinking`](routing.md#5-reasoning-effort-and-extended-thinking).

**extended thinking / thinking signature** — The reasoning trace produced by Claude when `ThinkingConfigEnabled` is active. The thinking block is streamed via `thinking` SSE events. Signatures are opaque tokens that must not be modified; muselab locks a session to a single model to avoid cross-vendor signature corruption. For Opus 4.7+, `display="summarized"` is required to get plaintext thinking blocks rather than signatures-only. See [`routing.md — budget_tokens and display`](routing.md#budget_tokens-and-displaysummarized).

**fork** — A copy of a session's transcript up to a chosen message UUID, stored under a new session ID. Used internally to implement message editing (the UI forks at the previous assistant turn, then re-sends). Both the JSONL and the `sessions/index.json` entry are created for the fork. See [`backend-sessions.md — Fork & edit-a-message`](backend-sessions.md#6-fork--edit-a-message).

**legacy session self-heal** — If a session was created before any provider was configured, it gets locked to the `MODEL` constant. When the user later configures only a third-party provider, every send 401s. On the next send, muselab detects that the locked model's provider is unavailable and the session has no on-disk JSONL (never ran), then re-resolves the model. Sessions that already have history are never re-resolved. See [`routing.md — Legacy-session self-heal`](routing.md#legacy-session-self-heal) and [`backend/chat.py:L1680`](../backend/chat.py#L1680).

**longest-prefix routing** — The algorithm muselab uses to map a model ID to its provider. `lookup(model)` in `backend/endpoints.py` sorts all provider prefixes longest-first and returns the first match, case-insensitively. Colon-tagged prefixes (e.g. `qwen-intl:`) are normalised before the model ID is sent to the vendor so the vendor never sees the routing tag. See [`backend/endpoints.py:L806`](../backend/endpoints.py#L806).

**MCP (Model Context Protocol)** — A standard for attaching external tool servers to the agent. muselab surfaces MCP configuration at `Settings → MCP` and merges its own `mcp.json` with Claude Code's global configs. The `ask_user_question` MCP tool is handled specially: muselab does not block it, instead re-routing it through an in-process queue so the browser can display the question inline. See [`mcp-architecture.md`](mcp-architecture.md).

**message queue** — A per-session FIFO queue (`sessions/<sid>.queue.json`) that holds prompts submitted while a turn is already running. The drain loop starts the next turn automatically when the current one finishes. Max depth is 10. The queue auto-pauses if a turn errors or is cancelled. See [`backend-sessions.md — The message queue`](backend-sessions.md#4-the-message-queue).

**model lock** — After the first successful turn, the model ID is written to `sessions/index.json` and used for every subsequent turn in that session, regardless of what the UI dropdown says. This prevents cross-vendor thinking-signature corruption. Changing the model via `PATCH /sessions/{sid}` updates the lock and disconnects the cached client. See [`routing.md — Session model lock`](routing.md#session-model-lock).

**no-build frontend** — The frontend is served as plain HTML + JavaScript + CSS with no bundler, compiler, or `npm install` required. Vetted third-party libraries are checked in under `frontend/vendor/`. Heavy libraries (KaTeX, CodeMirror, Mermaid, highlight.js) are lazy-loaded on first use. See [`architecture.md — No build step`](architecture.md#key-design-decisions).

**provider** — A vendor configuration record defined in `backend/endpoints.py`. Each provider has a `prefix` (used for longest-prefix routing), a `base_url`, an `env_key` (API key env var name), and flags like `supports_thinking`, `supports_effort`, and `max_output_tokens`. The built-in catalog covers 9 providers, including local-gateway presets; user-created providers use the `c:<slug>` stable ID format. See [`routing.md — Model Resolution`](routing.md#1-model-resolution) and [`providers.md`](providers.md).

**provider catalog** — The full list of available providers returned by `catalog()` in `backend/endpoints.py`. Built-in entries carry stable IDs prefixed `b:`. The catalog is cached by `(mtime_ns, size)` of `provider_overrides.json` and re-read on change. Claude (Anthropic) is managed separately from the catalog. See [`backend/endpoints.py:L170`](../backend/endpoints.py#L170).

**provider override** — A field-level patch stored in `provider_overrides.json` (repo root, next to `mcp.json`). Overrides can suppress a built-in provider, change its `base_url` or `env_key`, or define a fully custom provider. The fields `supports_thinking`, `supports_effort`, and `max_output_tokens` cannot be changed via the Settings UI. See [`add-provider.md`](add-provider.md) and [`routing.md — Model Resolution`](routing.md#1-model-resolution).

**PWA / service worker (push-only)** — muselab ships a `manifest.webmanifest` so browsers can install it as a Progressive Web App (standalone display mode, home-screen icon). The service worker (`frontend/sw.js`) deliberately does **not** cache any assets; its only function is to receive Web Push notifications and route them to the correct open tab. See [`mobile.md`](mobile.md).

**pending attachment queue** — A `pending_attachments` list in the sidecar that holds upload metadata until the SDK writes the user-message UUID. Because the CLI appends the JSONL record asynchronously, the message UUID is not known at upload time; muselab binds the attachment to the correct message UUID on the next session read. See [`backend-sessions.md — Pending attachment queue`](backend-sessions.md#pending-attachment-queue-pre-uuid-binding).

**repo root** — The directory containing the muselab checkout (`backend/`, `frontend/`, `sessions/`, `.env`, etc.). Distinct from the archive root. The repo is the install; the archive is your data. See [`architecture.md — Directory map`](architecture.md#directory-map).

**safe_resolve** — The path-validation function (`backend/files.py:L316`) that every `/api/files/*` endpoint calls before touching the filesystem. It blocks `..` traversal, symlink escapes, NUL byte injection, and sensitive filenames. All paths must resolve to a descendant of `MUSELAB_ROOT`. See [`backend-files.md — safe_resolve in depth`](backend-files.md#safe_resolve-in-depth) and [`backend/files.py:L316`](../backend/files.py#L316).

**scheduler** — A built-in asyncio cron loop (`backend/scheduler.py`) that runs saved prompts on a time-based schedule. State (tasks, next-run times, last-run history) is persisted to `$MUSELAB_ROOT/.muselab/scheduler.json`. Scheduled runs use the same client pool and SSE path as interactive turns. See [`scheduler.md`](scheduler.md).

**sensitive-filename blocklist** — Two sets in `backend/files.py` (`SENSITIVE_NAMES` and `SENSITIVE_SUFFIX`) covering credential files, private keys, shell history, and `.env` variants. Any path matching these is rejected with HTTP 403 by `safe_resolve` unless `allow_sensitive=True` is explicitly passed (used only by trash-restore and copy-bak). See [`backend-security.md — Filesystem containment`](backend-security.md#filesystem-containment) and [`backend/files.py:L286`](../backend/files.py#L286).

**session** — The top-level unit of a conversation. A session has a UUID that is shared by the muselab index entry, the sidecar file, the CLI JSONL, and the message queue file. A session is locked to one model after its first turn and carries its own effort and thinking settings. See [`backend-sessions.md`](backend-sessions.md).

**session index** — The file `sessions/index.json` (inside the repo, not the archive). It is muselab's source of truth for per-session metadata that the CLI does not track: model lock, system prompt, effort, thinking toggle, pinned state, and auto-named flag. The CLI JSONL (`~/.claude/projects/<cwd-key>/<sid>.jsonl`) is the source of truth for the conversation transcript. See [`backend-sessions.md — The session index`](backend-sessions.md#2-the-session-index).

**sidecar** — The file `sessions/<sid>.sidecar.json` that stores per-message annotations layered on top of the CLI JSONL: cost (USD), model badge, timestamps, uploaded image thumbnails, and document references. Written after every turn; never stores the transcript itself. See [`backend-sessions.md — Sidecar files`](backend-sessions.md#3-sidecar-files).

**setting_sources** — The SDK parameter `["user", "project", "local"]` passed in `ClaudeAgentOptions` that tells the Claude Agent SDK which config scopes to load. The `local` scope (relative to `cwd`, which is `MUSELAB_ROOT`) is how the bundled `skills/` directory and the archive's `CLAUDE.md` are discovered. See [`backend/chat.py:L944`](../backend/chat.py#L944).

**skill / SKILL.md** — A skill is a directory under `skills/` (or `~/.claude/skills/`) containing a `SKILL.md` file with YAML frontmatter. The `description` field (starting with `"USE WHEN …"`) is the primary signal the model uses to decide whether to activate the skill. With `skills="all"`, the Claude Agent SDK injects all discoverable SKILL.md files as additional context. Skills are disabled for third-party providers to avoid payload-size errors. See [`backend/chat.py:L958`](../backend/chat.py#L958).

**SSE / TurnBroadcast** — Chat turn output is delivered as a Server-Sent Events stream at `GET /api/chat/stream`. Internally, each turn is wrapped in a `TurnBroadcast` object that buffers all events so late subscribers (reconnecting browser tabs) get a full replay. The background pump task continues running even if the browser disconnects, so turns complete to disk regardless. See [`routing.md — TurnBroadcast: survive-disconnect design`](routing.md#turnbroadcast-survive-disconnect-design).

**trash** — A soft-delete staging area at `$MUSELAB_ROOT/.muselab-dustbin/`. Deleted files are moved here rather than permanently removed. Items auto-expire after `MUSELAB_TRASH_TTL_DAYS` (default 30). The `_guard_not_trash()` function blocks all write endpoints from targeting the dustbin directly; only the dedicated `/api/files/trash/*` endpoints may touch it. See [`backend-files.md — Trash semantics`](backend-files.md#trash-semantics).

**thinking toggle** — A per-session boolean (`thinking`, default `true`) that enables or disables `ThinkingConfigEnabled`. Setting it to `false` via `PATCH /sessions/{sid}` is the escape hatch for the "thinking blocks in the latest assistant message cannot be modified" 400 error that arises from certain tool-use interleaving patterns. Changing it invalidates the cached client. See [`routing.md — Reasoning Effort and Extended Thinking`](routing.md#5-reasoning-effort-and-extended-thinking).

**token (MUSELAB_TOKEN)** — A shared secret of at least 16 characters, set in `.env`, that gates every API endpoint. Comparisons use `hmac.compare_digest` (constant-time) to resist timing attacks. The token is accepted as an `X-Auth-Token` header for most endpoints and as a `?token=` query parameter for SSE and file-download endpoints where browsers cannot send custom headers. See [`backend-security.md — Authentication`](backend-security.md#authentication).

**vendored libraries** — Third-party JavaScript libraries checked in under `frontend/vendor/` so the frontend has no runtime npm dependency. Includes Alpine.js, marked, DOMPurify, Mermaid, highlight.js, KaTeX, and CodeMirror. Licenses are listed in [`THIRD_PARTY_LICENSES.md`](../THIRD_PARTY_LICENSES.md). See [`architecture.md — No build step`](architecture.md#key-design-decisions).

**vendor config dir** — See *CLAUDE_CONFIG_DIR isolation*.
