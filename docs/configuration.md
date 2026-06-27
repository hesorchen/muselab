# Configuration reference

> [简体中文](configuration_zh.md)

Every setting lives in the repo's `.env` file. The installer creates it; you can
edit it by hand, or change most values from the in-app **Settings** panel (which
hot-rewrites `.env` *and* the live process — no restart needed). Editing `.env`
by hand **does** require a restart, since the process only reads the file at
startup.

A starting template is `.env.example`.

## Authentication

muselab is single-user. One token guards the whole web UI and every API call.

- The token is `MUSELAB_TOKEN` in `.env`. Find it with `grep MUSELAB_TOKEN .env`.
- The browser sends it as the `X-Auth-Token` header (cached in
  `localStorage["muselab_token"]`); a `?token=` query parameter is also accepted
  for links.
- It must be at least 16 characters — the backend refuses to start otherwise.
  The installer generates a random one via `openssl rand -hex 32`.

## Core settings

| Variable | Controls | Default | Required |
|---|---|---|---|
| `MUSELAB_TOKEN` | Web-UI / API auth token | random (installer) | **Yes** — ≥16 chars |
| `MUSELAB_ROOT` | Absolute path to your archive (native runs) | — | **Yes** (native) |
| `MUSELAB_HOST` | Interface uvicorn binds to | `127.0.0.1` | No |
| `MUSELAB_PORT` | Listen port | `8765` | No |
| `MUSELAB_MODEL` | Default model id for new sessions | unset | No — **leave unset** so the UI auto-picks your first configured provider |

> `MUSELAB_ROOT` may not be a bare system path (`/`, `/etc`, `/home`, `/var`, …);
> the backend rejects those to avoid handing the agent your whole disk.

## Provider keys

Configure at least one. Anthropic works through `claude login` (Pro/Max OAuth) —
no key needed. Everything else is an API key. You can also set these from the
Settings panel.

| Provider | API-key env | Default base URL | Base-URL override |
|---|---|---|---|
| Anthropic (Claude) | `ANTHROPIC_API_KEY` *(or `claude login`)* | api.anthropic.com | — |
| DeepSeek | `DEEPSEEK_API_KEY` | api.deepseek.com/anthropic | `DEEPSEEK_BASE_URL` |
| 智谱 GLM | `ZHIPUAI_API_KEY` | open.bigmodel.cn/api/anthropic | `ZHIPUAI_BASE_URL` |
| MiniMax (China) | `MINIMAX_API_KEY` | api.minimaxi.com/anthropic | `MINIMAX_BASE_URL` |
| MiniMax (Global) | `MINIMAX_INTL_API_KEY` | api.minimax.io/anthropic | — |
| Kimi / Moonshot | `MOONSHOT_API_KEY` | api.moonshot.cn/anthropic | `MOONSHOT_BASE_URL` |
| Qwen / DashScope | `DASHSCOPE_API_KEY` | dashscope.aliyuncs.com/apps/anthropic | `DASHSCOPE_BASE_URL` |
| Xiaomi MiMo | `XIAOMI_MIMO_API_KEY` | api.xiaomimimo.com/anthropic | `XIAOMI_MIMO_BASE_URL` |
| Baidu ERNIE (Qianfan) | `QIANFAN_API_KEY` | qianfan.baidubce.com/anthropic | `QIANFAN_BASE_URL` |
| Codex Gateway | `CODEX_GATEWAY_API_KEY` | 127.0.0.1:8317 | `CODEX_GATEWAY_BASE_URL` |

Notes:
- **MiniMax China vs Global use different keys.** A `minimaxi.com` key 401s on
  `minimax.io` and vice-versa — set the one that matches your account.
- **Qwen** shares `DASHSCOPE_API_KEY` between its China and Global endpoints;
  the Global variant is selected per-model in the UI.
- Providers you add yourself in Settings get a key named
  `MUSELAB_PROVIDER_<SLUG>_API_KEY`.
- **Codex Gateway** is a local Anthropic-compatible sidecar. The token is for
  the gateway only; muselab does not store Codex OAuth credentials.
- **Image generation** is separate from chat providers. The default
  `MUSELAB_IMAGE_PROVIDER=auto` uses `OPENAI_IMAGE_API_KEY` (or
  `OPENAI_API_KEY`) when present. Local Codex `$imagegen` is an explicit opt-in:
  set `MUSELAB_IMAGE_PROVIDER=codex_imagegen` and `CODEX_IMAGEGEN_ENABLED=true`
  only on a trusted localhost instance. If your local gateway exposes
  OpenAI-compatible image endpoints, point `OPENAI_IMAGE_BASE_URL` at that
  `/v1` base URL and use the `openai` provider.

See [add-provider.md](add-provider.md) for adding an Anthropic-compatible
endpoint that isn't in this list.

## Optional tuning

All optional; sensible defaults apply if unset.

| Variable | Controls | Default |
|---|---|---|
| `MUSELAB_PROMPT_CACHE_TTL` | Claude prompt-cache TTL (`1h` / `5m` / empty=CLI default) | `1h` |
| `MUSELAB_BUDGET_USD` | Soft monthly budget — UI badge only, no hard stop | `0` (off) |
| `MUSELAB_MAX_UPLOAD_MB` | Max single upload size (MiB) | `100` |
| `MUSELAB_IMAGE_PROVIDER` | Composer image backend (`auto`, `openai`, `codex_imagegen`) | `auto` |
| `OPENAI_IMAGE_API_KEY` | API key for the composer GPT Image tool | unset |
| `OPENAI_IMAGE_BASE_URL` | OpenAI-compatible `/v1` base for image generation | `https://api.openai.com/v1` |
| `MUSELAB_IMAGE_GENERATION_TIMEOUT` | Image generation timeout, seconds | `180` |
| `CODEX_IMAGEGEN_ENABLED` | Allow local Codex `$imagegen` when `MUSELAB_IMAGE_PROVIDER=codex_imagegen` or `auto` has no image API key | `false` |
| `CODEX_IMAGEGEN_TIMEOUT_SECONDS` | Local Codex imagegen timeout, seconds | `300` |
| `MUSELAB_MAX_TURNS` | Max turns per session (0 = no cap) | `0` |
| `MUSELAB_THINKING_BUDGET` | Extended-thinking token budget (0 = off) | `10000` |
| `MUSELAB_CLIENT_POOL_CAP` | Pooled SDK clients kept warm | `3` |
| `MUSELAB_DISABLED_PROVIDERS` | Comma list of provider model-ids to hide | empty |
| `MUSELAB_DISABLE_SKILLS` | Turn off bundled skills (`1`/`true`) | off |
| `MUSELAB_PRUNE_EMPTY_SESSIONS` | Auto-delete sessions with no messages (`true`) | `false` |
| `MUSELAB_TRASH_TTL_DAYS` | Days to keep soft-deleted files in `.muselab-dustbin/` (0 = forever) | `30` |
| `MUSELAB_VAPID_SUBJECT` | Web-push VAPID `sub` claim (a `mailto:`) | `mailto:noreply@muselab.dev` |
| `MUSELAB_DEFAULT_PERMISSION` | Default permission mode | `bypassPermissions` |

> VAPID **keys** are not env vars — they're generated on disk at
> `<archive>/.muselab/vapid.json`. Only the subject above is configurable.

## Docker-only

Read by `docker-compose.yml`, **not** by the backend:

| Variable | Controls | Default |
|---|---|---|
| `ARCHIVE_DIR` | Host directory mounted to the container's `/data` | `./data` |
| `CLAUDE_HOME` | Host path to `~/.claude` (OAuth creds) | `${HOME}/.claude` |
| `MUSELAB_BIND` | Host interface for the published port | `127.0.0.1` |

## Install-time only

Read by the installer scripts, **not** by the running backend:

| Variable | Controls | Default |
|---|---|---|
| `MUSELAB_NONINTERACTIVE` | Take all defaults, skip every prompt | `0` |
| `MUSELAB_LOCALE` | Language for intake prompts + the seeded `CLAUDE.md` | auto (`LANG`) |

The running backend picks its language from `LANG` / `LC_ALL`, not
`MUSELAB_LOCALE`.

## Exposing muselab beyond localhost

The `127.0.0.1` defaults for `MUSELAB_HOST` (and Docker's `MUSELAB_BIND`) are a
safety floor: the only thing standing between the open internet and your archive
is the token. If you set either to `0.0.0.0`, put a reverse proxy with HTTPS in
front — see [Mobile / HTTPS](mobile.md) and `scripts/setup-https.sh`.
