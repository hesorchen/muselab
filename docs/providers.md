# Providers

> [简体中文](providers_zh.md)

muselab uses the **Claude Agent SDK** as the single chat backend. For
non-Claude models, a per-session env override routes the SDK at the
vendor's Anthropic-compatible endpoint. **Every provider gets the full
agent loop** — not just chat. No proxy, no protocol translation.

| Provider | How to enable | Tool use | Cost note |
|---|---|---|---|
| **Anthropic Claude** (Opus / Sonnet / Haiku) | `claude login` once | ✅ | Reuses Pro / Max OAuth — no API key, no per-token bill |
| **DeepSeek** (V4 Pro / V4 Flash / Chat / Reasoner) | `DEEPSEEK_API_KEY` in Settings | ✅ | ~10× cheaper than Claude for chat-heavy tasks |
| **智谱 GLM** (GLM-5 / GLM-5 Air / GLM-4.7 / 4 Plus) | `ZHIPUAI_API_KEY` | ✅ | Free tier on bigmodel.cn |
| **MiniMax** (M2.7 / M2.7 Highspeed / M2.5) | `MINIMAX_API_KEY` | ✅ | Note: returns thinking blocks by default |
| **Kimi** (K2 / K2.5 / K2.6 / K2 Thinking) | `MOONSHOT_API_KEY` | ✅ | platform.moonshot.cn |
| **Qwen** (Qwen3 Max / Plus / Flash / Coder) | `DASHSCOPE_API_KEY` | ✅ | dashscope.console.aliyun.com |
| **Xiaomi MiMo** (V2.5 Pro / V2 Flash) | `XIAOMI_MIMO_API_KEY` | ✅ | platform.xiaomimimo.com (beta) |

## Switching model mid-conversation

dropdown → confirm modal → spawns a fresh session with the new model. This
avoids cross-vendor thinking-signature drift, which causes silent breakage
when one provider's signed thinking blocks are sent back to a different
provider.

Each assistant message stores its own `model` field, so badges remain
accurate even after a page reload that re-renders the whole transcript.

## Adding a new provider

3 lines in `backend/endpoints.py`. See [add-provider.md](add-provider.md)
for the exact diff and the rationale behind the per-session
`CLAUDE_CONFIG_DIR` isolation.
