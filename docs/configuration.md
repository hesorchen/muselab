# Configuration reference

> [中文](configuration_zh.md)

muselab configuration has four layers. Not every setting belongs in `.env`:

1. **Deployment environment:** `.env` and process variables for binding, the primary workspace, credentials, and resource limits.
2. **Application configuration files:** provider, MCP, workspace, and terminal-profile JSON.
3. **Durable runtime state:** sessions, scheduler, Activity, Push, and image-generation jobs.
4. **Browser preferences:** language, layout, open tabs, and other local UI state.

Restart after editing `.env` manually. Provider keys, default model, and default permission saved in Settings are written atomically to the file selected by `MUSELAB_ENV_PATH` and to the current process, so they normally apply without restart. The UI does not expose the token, root directory, or bind address.

## Required and network settings

| Variable | Purpose | Default |
|---|---|---|
| `MUSELAB_TOKEN` | Shared UI/API token, at least 16 characters | Required |
| `MUSELAB_ROOT` | Primary workspace and root of global `.muselab` state | Required for native deployment |
| `MUSELAB_HOST` | uvicorn bind interface | `127.0.0.1` |
| `MUSELAB_PORT` | Listen port | `8765` |
| `MUSELAB_URL` | Optional public HTTPS origin for remote clients | Local origin |
| `MUSELAB_ENV_PATH` | `.env` file read and written by the Settings API; useful for tests or special deployments | `<repo>/.env` |
| `MUSELAB_MODEL` | Default model for new sessions; the built-in Claude default is effective when unset | `claude-sonnet-4-6` |
| `MUSELAB_DEFAULT_MODEL` | Settings-managed default, synchronized with `MUSELAB_MODEL` | `claude-sonnet-4-6` |
| `MUSELAB_DEFAULT_PERMISSION` | Default SDK permission mode | `bypassPermissions` |

`MUSELAB_ROOT` must exist. System-level roots such as `/`, `/etc`, `/root`, `/home`, `/var`, `/usr`, and `/boot` are rejected. A user's own home directory or a directory beneath it is allowed.

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

Built-in edits, custom providers, and deletion state are stored in `<repo>/provider_overrides.json`. MCP server configuration is stored in `<repo>/mcp.json`. Custom-provider keys use `MUSELAB_PROVIDER_<SLUG>_API_KEY`.

## Image generation

| Variable | Purpose | Default |
|---|---|---|
| `MUSELAB_IMAGE_PROVIDER` | `auto`, `openai`, or `codex_imagegen` | `auto` |
| `OPENAI_IMAGE_API_KEY` | OpenAI Image API key; may fall back to `OPENAI_API_KEY` | Empty |
| `OPENAI_IMAGE_BASE_URL` | OpenAI-compatible `/v1` base URL | `https://api.openai.com/v1` |
| `MUSELAB_IMAGE_GENERATION_TIMEOUT` | Image API timeout in seconds | `180` |
| `CODEX_IMAGEGEN_ENABLED` | Allow a local Codex image process | `false` |
| `CODEX_IMAGEGEN_TIMEOUT_SECONDS` | Local Codex timeout | `300` |

Local Codex image generation is suitable only for trusted instances. Durable jobs and files live under `$MUSELAB_ROOT/.muselab/imagegen/`.

## Resource and behavior tuning

| Variable | Purpose | Default |
|---|---|---|
| `MUSELAB_PROMPT_CACHE_TTL` | Claude prompt-cache TTL | `1h` |
| `MUSELAB_BUDGET_USD` | Monthly UI soft budget; does not hard-stop turns | `0` |
| `MUSELAB_MAX_UPLOAD_MB` | Files API upload limit per file in MiB | `100` |
| `MUSELAB_MAX_TURNS` | Additional session turn cap; `0` means unlimited | `0` |
| `MUSELAB_THINKING_BUDGET` | Extended-thinking token budget | `10000` |
| `MUSELAB_CLIENT_POOL_CAP` | Number of live SDK clients | `3` |
| `MUSELAB_RECENT_TURN_TTL` | Seconds a finished turn remains reconnectable | `60` |
| `MUSELAB_STREAM_REPLAY_MAX_EVENTS` | Mobile replay event threshold before resync | `512` |
| `MUSELAB_STREAM_REPLAY_MAX_BYTES` | Mobile replay byte threshold before resync | `2097152` |
| `MUSELAB_DISABLED_PROVIDERS` | Comma-separated stable provider IDs to hide | Empty |
| `MUSELAB_DISABLE_SKILLS` | Disable Skills | `0` |
| `MUSELAB_PRUNE_EMPTY_SESSIONS` | Prune sessions meeting strict empty-session rules | `false` |
| `MUSELAB_TRASH_TTL_DAYS` | Dustbin retention; `0` means forever | `30` |
| `MUSELAB_VAPID_SUBJECT` | Web Push VAPID subject | `mailto:noreply@muselab.dev` |

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
| `ARCHIVE_DIR` | Host directory mounted at container `/data` | `./data` |
| `CLAUDE_HOME` | Host Claude CLI configuration directory | `${HOME}/.claude` |
| `MUSELAB_BIND` | Host interface for the published port | `127.0.0.1` |

## Install-time settings

| Variable | Purpose | Default |
|---|---|---|
| `MUSELAB_NONINTERACTIVE` | Use install defaults without prompts | `0` |
| `MUSELAB_LOCALE` | Installer and initial-template language | Detected from `LANG` |
| `MUSELAB_SKIP_SERVICE` | Install files and dependencies without registering or starting systemd/launchd | `0` |
| `MUSELAB_NO_BROWSER` | Do not open a browser after installation | `0` |

The browser stores the runtime language preference. Backend template selection considers `LANG`, `LC_ALL`, and `LC_MESSAGES`.

## Exposing the service

`MUSELAB_HOST` and Docker's `MUSELAB_BIND` default to localhost. When setting either to `0.0.0.0`, use an HTTPS reverse proxy, firewall, and dedicated unprivileged service user. A leaked token has a much larger impact when real terminals are enabled than it would on a read-only notes site.
