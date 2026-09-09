# Configuration reference

> [中文](configuration_zh.md)

muselab configuration has four layers. Not every setting belongs in `.env`:

1. **Deployment environment:** `.env` and process variables for binding, the primary workspace, credentials, and resource limits.
2. **Application configuration files:** provider, MCP, workspace, and terminal-profile JSON.
3. **Durable runtime state:** sessions, scheduler, Activity, Push, and image-generation jobs.
4. **Browser preferences:** language, layout, open tabs, and other local UI state.

Restart after editing `.env` manually. Provider keys, default model, default permission, and busy-send behavior saved in Settings are written atomically to the file selected by `MUSELAB_ENV_PATH` and to the current process, so they normally apply without restart. The UI does not expose the token, root directory, or bind address.

## Required and network settings

| Variable | Purpose | Default |
|---|---|---|
| `MUSELAB_TOKEN` | Shared UI/API token, at least 16 characters | Required |
| `MUSELAB_ROOT` | Primary workspace and root of global `.muselab` state | Required for native deployment |
| `MUSELAB_HOST` | uvicorn bind interface | `127.0.0.1` |
| `MUSELAB_PORT` | Listen port | `8765` |
| `MUSELAB_URL` | Optional public HTTPS origin for remote clients | Local origin |
| `MUSELAB_CONFIG_DIR` | Directory for mutable `.env`, MCP and provider configuration; set in the process environment before startup | `<repo>`; Docker `/app/sessions/config` |
| `MUSELAB_ENV_PATH` | Explicit `.env` path, read at startup and written by Settings; set in the process environment | `$MUSELAB_CONFIG_DIR/.env` |
| `MUSELAB_ENV_OVERRIDE` | Whether the selected file overrides existing process values on startup | `0`; Docker `1` so saved UI values survive recreation |
| `MUSELAB_SESSIONS_DIR` | Durable session metadata directory | `<repo>/sessions` |
| `MUSELAB_MODEL` | Default model for new sessions; the built-in Claude default is effective when unset | `claude-sonnet-4-6` |
| `MUSELAB_DEFAULT_MODEL` | Settings-managed default, synchronized with `MUSELAB_MODEL` | `claude-sonnet-4-6` |
| `MUSELAB_DEFAULT_PERMISSION` | Default SDK permission mode | `bypassPermissions` |
| `MUSELAB_BUSY_SEND_MODE` | Busy-session send behavior: `adjust` steers the current task at the next safe tool boundary; `queue` waits for the current turn to finish | `adjust` |
| `MUSELAB_MEMORY_DIR` | Optional long-term-memory Registry/config directory | `$MUSELAB_ROOT/.muselab/memory` |

`MUSELAB_ROOT` must exist. System-level roots such as `/`, `/etc`, `/root`, `/home`, `/var`, `/usr`, and `/boot` are rejected. A user's own home directory or a directory beneath it is allowed. Older documentation called this the archive root; the environment-variable name remains compatible, while the product concept is now the primary workspace.

`MUSELAB_SESSIONS_DIR` stores muselab's session index, sidecars, queues, and
restart-recovery sentinels, not the CLI transcript itself. Native installers
write the checkout's absolute `<repo>/sessions` path into `.env`; an unset
value retains the repo-local default for compatibility. Docker additionally
sets `XDG_STATE_HOME=/app/sessions/state`, placing third-party CLI transcripts
under the same persistent mount. Its `config/` child stores editable settings.
See [legacy Docker migration](docker-state-migration.md) before replacing an older container.

## Multiple workspaces

Additional workspaces are registered through the UI and stored in:

```text
$MUSELAB_ROOT/.muselab/workspaces.json
```

Switching workspaces changes the file tree, previews, terminal working directory, and new-session `cwd` together. Session history is scanned across every registered root. Registration is not OS isolation; add only paths you intend to expose to the service user and Web UI.

## Providers

Claude can use `claude login` or `ANTHROPIC_API_KEY`. Built-in Anthropic-compatible providers use:

| Provider | Key | Optional endpoint override |
|---|---|---|
| DeepSeek | `DEEPSEEK_API_KEY` | `DEEPSEEK_BASE_URL` |
| Zhipu GLM / built-in OpenAI group | `ZHIPUAI_API_KEY` | `ZHIPUAI_BASE_URL` |
| MiniMax China | `MINIMAX_API_KEY` | `MINIMAX_BASE_URL` |
| MiniMax international | `MINIMAX_INTL_API_KEY` | Provider configuration |
| Kimi | `MOONSHOT_API_KEY` | `MOONSHOT_BASE_URL` |
| Qwen China | `DASHSCOPE_API_KEY` | `DASHSCOPE_BASE_URL` |
| Qwen international | `DASHSCOPE_API_KEY` | `DASHSCOPE_INTL_BASE_URL` |
| Xiaomi MiMo | `XIAOMI_MIMO_API_KEY` | `XIAOMI_MIMO_BASE_URL` |
| Baidu Qianfan | `QIANFAN_API_KEY` | `QIANFAN_BASE_URL` |
| Codex Gateway | `CODEX_GATEWAY_API_KEY` | `CODEX_GATEWAY_BASE_URL` |

MiniMax China and international keys are not interchangeable. The two Qwen regions share a key but use different endpoints. Models and groups change over time; Settings and `/api/chat/providers` are the current source of truth.

Built-in edits, custom providers, and deletion state are stored in `$MUSELAB_CONFIG_DIR/provider_overrides.json`. MCP server configuration is stored in `$MUSELAB_CONFIG_DIR/mcp.json` (both remain repo-local on native installs by default). Custom-provider keys use `MUSELAB_PROVIDER_<SLUG>_API_KEY`.

## Long-term memory

Long-term memory is off by default. Its structured configuration lives in
`$MUSELAB_MEMORY_DIR/config.json` (default:
`$MUSELAB_ROOT/.muselab/memory/config.json`). Embedding, vector-store, and
reranker credentials are not written to `.env` and are never returned in
plaintext by Settings. See [Long-term memory](memory.md) for modes, providers,
and backup requirements.

## Image generation

| Variable | Purpose | Default |
|---|---|---|
| `OPENAI_IMAGE_API_KEY` | Images API key; may fall back to `OPENAI_API_KEY` | Empty |
| `OPENAI_IMAGE_BASE_URL` | OpenAI-compatible `/v1` base URL, including self-hosted adapters | `https://api.openai.com/v1` |
| `MUSELAB_IMAGE_GENERATION_TIMEOUT` | Image API timeout in seconds | `180` |

muselab only acts as an Images API client; it does not launch a local image model or Codex process. Durable jobs and files live under `$MUSELAB_ROOT/.muselab/imagegen/`.

## Resource and behavior tuning

| Variable | Purpose | Default |
|---|---|---|
| `MUSELAB_PROMPT_CACHE_TTL` | Claude prompt-cache TTL | `1h` |
| `MUSELAB_BUDGET_USD` | Monthly UI soft budget; does not hard-stop turns | `0` |
| `MUSELAB_PERF_LOG` | Emit privacy-bounded performance summaries for core paths; set to `0` to disable | `1` |
| `MUSELAB_SLOW_REQUEST_MS` | Duration threshold for slow HTTP and workspace-operation summaries, clamped to 25–60000 ms | `500` |
| `MUSELAB_MAX_UPLOAD_MB` | Files API upload limit per file in MiB | `1024` |
| `MUSELAB_MAX_TURNS` | Additional session turn cap; `0` means unlimited | `0` |
| `MUSELAB_THINKING_BUDGET` | Extended-thinking token budget | `10000` |
| `MUSELAB_RUNTIME_BUFFER_EVENTS` | Queued SDK messages per turn/background handoff, minimum 1 | `8192` |
| `MUSELAB_RUNTIME_BUFFER_BYTES` | Estimated parsed-object bytes per SDK handoff, minimum 1024 | `67108864` |
| `MUSELAB_CLIENT_POOL_CAP` | Number of live SDK clients | `3` |
| `MUSELAB_RECENT_TURN_TTL` | Seconds a finished turn remains reconnectable | `60` |
| `MUSELAB_INTERRUPT_ACK_TIMEOUT_S` | Graceful SDK interrupt acknowledgement timeout | `0.35` |
| `MUSELAB_INTERRUPT_FORCE_GRACE_S` | Deadline from Stop click before forced client teardown | `0.5` |
| `MUSELAB_STREAM_REPLAY_MAX_EVENTS` | Mobile replay event threshold before resync | `512` |
| `MUSELAB_STREAM_REPLAY_MAX_BYTES` | Mobile replay byte threshold before resync | `2097152` |
| `MUSELAB_DISABLED_PROVIDERS` | Comma-separated stable provider IDs to hide | Empty |
| `MUSELAB_DISABLE_SKILLS` | Disable Skills | `0` |
| `MUSELAB_PRUNE_EMPTY_SESSIONS` | Prune sessions meeting strict empty-session rules | `false` |
| `MUSELAB_TRASH_TTL_DAYS` | Dustbin retention; `0` means forever | `30` |
| `MUSELAB_VAPID_SUBJECT` | Web Push VAPID subject | `mailto:noreply@muselab.dev` |

The SDK handoff budgets apply before browser SSE replay. The orphan handoff
also has a 512-message cap. A single oversized message or accumulated backlog
produces `runtime_buffer_exceeded`, interrupts/disconnects the exact owning SDK
client, and enters the existing failed-turn/history recovery path; messages are
never silently evicted and the sole SDK reader never waits for queue space.
These limits are estimated Python-object memory, not token or upload limits.
Changes take effect after a service restart.

The Service panel reads numeric `diagnostics.runtime_buffers` from
`GET /api/settings/service`: current depth/estimated bytes, oldest wait,
maximum observed single-queue depth/bytes and dequeue wait, and process-lifetime
overflow count. No prompt, tool result, path, or protocol payload is logged.
MCP status/reconnect observe a shared three-second deadline with at most four
in-flight controls; partial responses mark unfinished operations `pending`.
The SDK owns their completion timeout, and repeated calls reuse an in-flight
operation instead of cancelling or duplicating it.

The VAPID keypair is not an environment variable. It is generated at `$MUSELAB_ROOT/.muselab/vapid.json`.

## Real terminals

| Variable | Purpose | Default |
|---|---|---|
| `MUSELAB_TERMINAL_ENABLED` | Enable real PTY terminals | `1` |
| `MUSELAB_TERMINAL_SHELL` | Shell executable | `$SHELL`, then bash/zsh/sh |
| `MUSELAB_TERMINAL_MAX_SESSIONS` | Retained terminal limit, range 1–32 | `8` |
| `MUSELAB_TERMINAL_BUFFER_BYTES` | Replay bytes per terminal, range 64 KiB–16 MiB | `2097152` |
| `MUSELAB_TERMINAL_DETACHED_TTL` | Reap a running terminal with no subscribers after this many seconds | `1800` |
| `MUSELAB_TERMINAL_EXITED_TTL` | Keep an exited terminal in the list for this many seconds | `3600` |

Terminals run with the real authority of the service user and start in the active workspace. They are not constrained by the Files API sandbox. The shell receives only basic system variables, not the muselab token or provider keys.

Profiles are stored in `$MUSELAB_ROOT/.muselab/terminal_profiles.json` and shared across workspaces. An omitted `profile_id` uses the default profile; an explicit empty string starts a plain shell. The command runs after the interactive shell starts. Do not store passwords or API keys in profile commands.

## Docker Compose

These variables are consumed by `docker-compose.yml`, not by backend business logic:

| Variable | Purpose | Default |
|---|---|---|
| `ARCHIVE_DIR` | Host workspace mounted at container `/data`; the name is retained only for Docker Compose compatibility | `./data` |
| `CLAUDE_HOME` | Host Claude CLI configuration directory | `${HOME}/.claude` |
| `MUSELAB_BIND` | Host interface for the published port | `127.0.0.1` |

## Install-time settings

| Variable | Purpose | Default |
|---|---|---|
| `MUSELAB_NONINTERACTIVE` | Use install defaults without prompts | `0` |
| `MUSELAB_LOCALE` | Language used by optional `scripts/intake.sh` workspace templates | Detected from `LANG` |
| `MUSELAB_SKIP_SERVICE` | Install files and dependencies without registering or starting systemd/launchd | `0` |
| `MUSELAB_NO_BROWSER` | Do not open a browser after installation | `0` |

The browser stores the runtime language preference. Backend template selection considers `LANG`, `LC_ALL`, and `LC_MESSAGES`.

## Exposing the service

`MUSELAB_HOST` and Docker's `MUSELAB_BIND` default to localhost. When setting either to `0.0.0.0`, use an HTTPS reverse proxy, firewall, and dedicated unprivileged service user. A leaked token has a much larger impact when real terminals are enabled than it would on a read-only notes site.

## Additional index exclusions

`MUSELAB_IGNORED_SUBTREES` adds comma-separated exact directory names to the built-in index exclusions. `MUSELAB_IGNORED_SUBTREE_PREFIXES` adds comma-separated directory-name prefixes. Matching is case-sensitive at every depth; whitespace and empty entries are ignored. For example, `MUSELAB_IGNORED_SUBTREES=bulk,generated` and `MUSELAB_IGNORED_SUBTREE_PREFIXES=snapshot.,backup.` keep their descendants out of recursive indexing and native watches. The directory itself may remain visible as an opaque entry.

Restart after changing these variables. The next complete reconciliation removes previously indexed descendants; removing an exclusion restores them on reconciliation. This never deletes source files and does not guarantee that the SQLite file shrinks: database compaction is a separate maintenance operation. Avoid broad prefixes that also match working documents.

## Context budgets in Settings

Settings → Providers → Context window budget accepts an effective token budget per provider (including DUCC) or per model. A model override wins over its provider default. Leave the field blank and save to remove that override. Changes are saved in `MUSELAB_CONTEXT_LIMITS` as JSON in the configured `.env` and apply to subsequent budget calculations without restarting. Existing explicit environment overrides take precedence; otherwise the runtime/catalog and model fallback remain automatic.

These budgets control MuseLab usage display and preflight/compaction decisions, not the upstream model's physical capacity. A higher value cannot unlock a larger model window. DUCC is a separate CLI route: its reported SDK window wins in automatic mode, with a 128000-token fallback for unmapped prefixed IDs. It must not inherit the capacity of a similarly named direct API model. The meter identifies settings overrides separately from SDK, catalog and estimated fallback sources.

Memory maintenance reuses an active reindex operation for the same owner, semantic memory snapshot and configuration revision, including after a process restart. Dream submissions reuse a queued/running job for the same episode set and revision. New content or configuration is a distinct request. After all batches reach a terminal state, a new explicit submission may run again. The diagnostics panel shows recent owner-scoped jobs, retry attempts, safe failure categories, pending index count and latest reindex batch progress; refresh it to read current status. It never displays job payloads or raw exception text.
