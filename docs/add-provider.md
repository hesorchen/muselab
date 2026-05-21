# Adding a new LLM provider

> [简体中文](add-provider_zh.md)

muselab is not Claude-locked. Any vendor that exposes an
**Anthropic Messages API-compatible endpoint** can be wired in with
**3 steps, 3 lines of config, ~5 minutes**. All Claude SDK capabilities
(Read/Edit/Bash/Grep/MCP/Skills/CLAUDE.md auto-load) work across vendors
automatically.

## Prerequisite: check whether the vendor has an Anthropic-compatible endpoint

Search the vendor's docs for "Anthropic compatible" / "anthropic-compatible"
/ "/anthropic". Since 2026, most Chinese LLM vendors support it. Currently
known:

| Vendor | Anthropic endpoint | Status |
|--------|-------------------|--------|
| DeepSeek | `https://api.deepseek.com/anthropic` | ✅ built-in |
| 智谱 GLM | `https://open.bigmodel.cn/api/anthropic` | ✅ built-in |
| MiniMax | `https://api.minimaxi.com/anthropic` | ✅ built-in |
| Xiaomi MiMo | – | ❌ OpenAI protocol only |
| Qwen | – | ❌ OpenAI protocol only |

**Vendors without an Anthropic endpoint** aren't supported. Either push the
vendor to ship a compatible endpoint, or use
[claude-code-router](https://github.com/musistudio/claude-code-router) as a
protocol translator (lossy; needs an extra process).

---

## The 3 steps

### 1. Add a catalog entry in `backend/endpoints.py`

```python
# backend/endpoints.py — append to the CATALOG tuple
Provider(
    prefix="qwen-",                                       # model name prefix (dispatcher uses this)
    base_url="https://dashscope.aliyuncs.com/anthropic",  # vendor's Anthropic endpoint
    env_key="DASHSCOPE_API_KEY",                          # corresponding .env key
    display="Qwen",                                       # UI group name
    models=("qwen-3.6-max", "qwen-3.6-plus"),             # specific models exposed in the dropdown
),
```

### 2. Add the API key in `.env`

```bash
echo "DASHSCOPE_API_KEY=sk-xxx" >> .env
```

Or paste it via the Settings modal UI (safer — it writes to file and
refreshes `os.environ` for you).

### 3. Restart the service

```bash
# Docker
docker compose restart

# Or native
# kill the old uvicorn, then uv run uvicorn ...
```

Done. The browser **model dropdown** will show a new "Qwen" group. Pick it
and you can chat + use tools immediately.

---

## How it works

```
muselab receives a chat request
  ↓
chat.py looks at the model prefix
  ├── claude-*  → ClaudeSDKClient (no env override)
  │              → goes to Anthropic API via your Pro OAuth credentials
  │
  └── matches a catalog prefix → ClaudeSDKClient (env override)
                                → swaps ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN
                                → SDK thinks it's still talking to Anthropic,
                                  the request actually hits the vendor endpoint
                                → vendor endpoint translates Anthropic protocol
                                  to its own, and back on the response
```

**Key point**: muselab business code stays untouched, the SDK doesn't know
either. The env override is passed to the underlying claude CLI subprocess
via `ClaudeAgentOptions(env=...)` on each `get_client(session_id, model, ...)`
call.

---

## Testing a new provider

```bash
# 1. Verify the endpoint reachable
curl https://your-vendor.com/anthropic/v1/messages -X POST \
  -H "Authorization: Bearer sk-xxx" \
  -H "Content-Type: application/json" \
  -d '{"model": "your-model", "messages": [{"role":"user","content":"hi"}], "max_tokens": 50}'

# 2. In the muselab UI, pick this model and send a message
# 3. Check whether tool calls fire (ask it to "Read README.md")
```

If **chat works but tool calls don't**, the vendor's Anthropic-compatible
endpoint likely hasn't implemented tool use. File an issue with them, or
treat it as a chat-only provider for now.

---

## Known gotchas

### DeepSeek thinking mode

DeepSeek reasoning models (`deepseek-reasoner`) require `reasoning_content`
to be echoed back on the next turn. When SDK talks the Anthropic protocol,
that mapping is handled by DeepSeek's endpoint; if that ever breaks
context across turns, the workaround is to disable thinking or switch to
a chat model.

### Pro OAuth stays untouched

Only models whose prefix matches a catalog entry get the env override.
Claude models (`claude-*`) **don't** go through override — they keep using
`claude login`'s OAuth, so you never pay API fees.

### Don't forget tests

When adding a provider, please add to `tests/test_endpoints.py`:

```python
@pytest.mark.parametrize("model,expected_host", [
    ("qwen-3.6-max", "dashscope.aliyuncs.com"),   # your vendor
])
def test_provider_routing_correct(monkeypatch, model, expected_host):
    ep = _reload_endpoints(monkeypatch, {})
    assert expected_host in ep.lookup(model).base_url
```

Run `make test` to make sure nothing regressed.

---

## FAQ

**Q: Does the vendor require a top-up first?**
A: Yes. muselab doesn't manage your bills. Only Pro OAuth uses the
subscription's included quota.

**Q: Can one session switch vendors mid-conversation?**
A: Yes. Sessions are model-agnostic — switching the dropdown takes effect
immediately, history is preserved. But cross-vendor switches mean the new
model can't see the other vendor's internal `tool_use` context. Plain text
chat works fine.

**Q: Are `prefix` and `models` redundant in the catalog?**
A: `prefix` is what the dispatcher matches on; `models` is the explicit
list shown in the UI dropdown. Every value in `models` must start with
`prefix`.

**Q: Do I need to restart after editing the catalog?**
A: Yes. It's a Python module-level constant; hot reload is a separate
engineering problem.

**Q: Smart routing between vendors (e.g. plan tasks → Sonnet, code → DeepSeek)?**
A: Don't do it inside muselab.
[claude-code-router](https://github.com/musistudio/claude-code-router) is
the right place. muselab's design philosophy is "thin layer + user picks
the model".
