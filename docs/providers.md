# Providers

> [简体中文](providers_zh.md)

muselab uses the **Claude Agent SDK** as the single chat backend. For
non-Claude models, a per-session env override routes the SDK at the
vendor's Anthropic-compatible endpoint. **Every provider gets the full
agent loop** — not just chat. muselab itself never implements OpenAI-native
protocols; if a backend is not Anthropic-compatible, put a gateway in front
of it.

| Provider | How to enable | Tool use | Where to get the key |
|---|---|---|---|
| **Anthropic Claude** (Opus / Sonnet / Haiku) | `claude login` once | ✅ | Reuses Pro / Max OAuth — no API key, no per-token bill |
| **DeepSeek** (V4 series) | `DEEPSEEK_API_KEY` in Settings | ✅ | platform.deepseek.com |
| **智谱 GLM** (GLM 5 / 5 Air / 5.1 / 4.7 / 4 Plus) | `ZHIPUAI_API_KEY` | ✅ | bigmodel.cn (free tier available) |
| **MiniMax** (M2.1 / M2.5 / M2.7 + Highspeed variants; 国际 endpoint via `MINIMAX_INTL_API_KEY`) | `MINIMAX_API_KEY` | ✅ | minimaxi.com (国内) / minimax.io (国际) — returns thinking blocks by default |
| **Kimi** (K2 / K2.5 / K2.6 / K2 Thinking) | `MOONSHOT_API_KEY` | ✅ | platform.moonshot.cn |
| **Qwen** (Qwen3 / 3.5 / 3.6 series — Max / Plus / Flash / Coder; 国际 endpoint via same key) | `DASHSCOPE_API_KEY` | ✅ | dashscope.console.aliyun.com — one key works for 国内 + 国际 (latency-only difference) |
| **Xiaomi MiMo** (V2.5 Pro / V2.5 / V2 Flash) | `XIAOMI_MIMO_API_KEY` | ✅ | platform.xiaomimimo.com (beta) |
| **Baidu Qianfan** (ERNIE 4 / 4.5 / 5 series + X1 reasoning + DeepSeek V3.2 via Qianfan) | `QIANFAN_API_KEY` | ✅ | console.bce.baidu.com/qianfan — Anthropic-compat path needs an IAM **access token** (`bce-v3/ALTAK-xxx/xxx`), not a plain `sk-xxx` key |
| **Codex Gateway** (local sidecar) | `CODEX_GATEWAY_API_KEY` | ✅* | A user-run Anthropic-compatible gateway at `127.0.0.1`; see [codex-gateway.md](codex-gateway.md) |

\* Tool use depends on the gateway translating Anthropic `tool_use` / `tool_result` correctly.

Exact model ids in each family come from the UI dropdown — they're sourced
from the effective catalog (built-in defaults + your Settings overrides) and
may evolve faster than this table.

## Image generation

The composer image button is not a chat provider. `MUSELAB_IMAGE_PROVIDER=auto`
uses the native OpenAI Image API when `OPENAI_IMAGE_API_KEY` (or
`OPENAI_API_KEY`) is configured. The local Codex `$imagegen` path is explicit
opt-in: set `MUSELAB_IMAGE_PROVIDER=codex_imagegen` and
`CODEX_IMAGEGEN_ENABLED=true` to use the logged-in `codex` CLI. Set
`MUSELAB_IMAGE_PROVIDER=openai` to force the OpenAI-compatible path.

Generated images are staged as normal muselab image attachments, so they can be
previewed, annotated, and sent into the current chat. Image requests run as
background jobs and are also kept in the image history drawer, so refreshing the
page does not lose completed outputs. The Codex imagegen path is intended for
localhost single-user deployments; do not expose a muselab instance with local
Codex access to the public internet.

## Switching model mid-conversation

If the current session already has messages, the dropdown opens a confirm
modal and forks a fresh session with the new model — the original is kept
in history. Empty sessions switch in place (no fork). The fork avoids
cross-vendor thinking-signature drift, which causes silent breakage when
one provider's signed thinking blocks are sent back to a different
provider.

Each assistant message stores its own `model` field, so badges remain
accurate even after a page reload that re-renders the whole transcript.

## Adding a new provider

Add one in **Settings → Providers** — endpoint, prefix, model list, and key;
it takes effect immediately, no restart. Providers can also be shipped as
built-in defaults via a `CATALOG` entry in `backend/endpoints.py` (the
contributor path). See [add-provider.md](add-provider.md) for both paths and
the rationale behind the per-session `CLAUDE_CONFIG_DIR` isolation.
