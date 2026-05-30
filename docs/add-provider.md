# Adding a new LLM provider

> [简体中文](add-provider_zh.md)

muselab is not locked to Claude. Any vendor that exposes an
**Anthropic Messages API-compatible endpoint** can be integrated in
**3 steps and a single catalog entry**. All Claude SDK
capabilities (Read/Edit/Bash/Grep/MCP/Skills/CLAUDE.md auto-load) work
across vendors automatically.

## Prerequisite: check whether the vendor has an Anthropic-compatible endpoint

Search the vendor's documentation for "Anthropic compatible", "anthropic-compatible",
or "/anthropic". As of 2026, most Chinese LLM vendors support this interface. Currently
known integrations:

| Vendor | Anthropic endpoint | Status |
|--------|-------------------|--------|
| DeepSeek | `https://api.deepseek.com/anthropic` | ✅ built-in |
| 智谱 GLM | `https://open.bigmodel.cn/api/anthropic` | ✅ built-in |
| MiniMax | `https://api.minimaxi.com/anthropic` | ✅ built-in |
| Kimi (Moonshot) | `https://api.moonshot.cn/anthropic` | ✅ built-in |
| Qwen (DashScope) | `https://dashscope.aliyuncs.com/apps/anthropic` (domestic default; international group uses `dashscope-intl.aliyuncs.com`) | ✅ built-in |
| Xiaomi MiMo | `https://api.xiaomimimo.com/anthropic` | ✅ built-in |
| Baidu Qianfan (ERNIE) | `https://qianfan.baidubce.com/anthropic` | ✅ built-in |

**Vendors without an Anthropic endpoint** are not supported. Options are to
request that the vendor ship a compatible endpoint, or to use
[claude-code-router](https://github.com/musistudio/claude-code-router) as a
protocol translator (lossy; requires an additional process).

---

## Integration steps

### 1. Add a catalog entry in `backend/endpoints.py`

```python
# backend/endpoints.py — append to the CATALOG tuple. Fictional vendor
# below — replace with your real values. See existing entries in CATALOG
# for working real-world examples (DeepSeek / GLM / MiniMax / etc.).
Provider(
    prefix="acme-",                                  # model name prefix (dispatcher uses this)
    base_url="https://api.acme.com/anthropic",       # vendor's Anthropic-compatible endpoint
    env_key="ACME_API_KEY",                          # corresponding .env key
    display="Acme",                                  # UI group name
    models=(
        ("acme-large", "Large"),                     # (model_id, UI label)
        ("acme-small", "Small"),
    ),
),
```

### 2. Add the API key in `.env`

```bash
echo "ACME_API_KEY=sk-xxx" >> .env
```

Alternatively, paste it via the Settings modal in the UI (recommended — it
writes to file and refreshes `os.environ` automatically).

### 3. Restart the service

```bash
# Docker
docker compose restart

# Or native
# kill the old uvicorn, then uv run uvicorn ...
```

The browser **model dropdown** will now show a new "Acme" group. Select it
to start chatting and using tools immediately.

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
                                → sets ANTHROPIC_BASE_URL + ANTHROPIC_API_KEY
                                  (also mirrors to ANTHROPIC_AUTH_TOKEN for
                                  vendors that accept Bearer instead of
                                  x-api-key), and points CLAUDE_CONFIG_DIR
                                  at an isolated dir so the CLI cannot
                                  fall back to Pro OAuth
                                → SDK thinks it's still talking to Anthropic,
                                  the request actually hits the vendor endpoint
                                → vendor endpoint translates Anthropic protocol
                                  to its own, and back on the response
```

**Key point**: muselab application code is unchanged, and the SDK is unaware
of the redirection. The env override is passed to the underlying claude CLI
subprocess via `ClaudeAgentOptions(env=...)` on each
`get_client(session_id, model, ...)` call.

---

## Testing a new provider

```bash
# 1. Verify the endpoint is reachable
curl https://your-vendor.com/anthropic/v1/messages -X POST \
  -H "Authorization: Bearer sk-xxx" \
  -H "Content-Type: application/json" \
  -d '{"model": "your-model", "messages": [{"role":"user","content":"hi"}], "max_tokens": 50}'

# 2. In the muselab UI, pick this model and send a message
# 3. Check whether tool calls fire (ask it to "Read README.md")
```

If **chat works but tool calls do not**, the vendor's Anthropic-compatible
endpoint has likely not implemented tool use. File an issue with the vendor,
or treat it as a chat-only provider in the meantime.

---

## Known gotchas

### Pro OAuth stays untouched

Only models whose prefix matches a catalog entry receive the env override.
Claude models (`claude-*`) do not go through the override — they continue
using the OAuth credentials from `claude login`, so no API fees are incurred.

### Add tests

When adding a provider, add a corresponding test in `tests/test_endpoints.py`:

```python
@pytest.mark.parametrize("model,expected_host", [
    ("qwen3-max", "dashscope.aliyuncs.com"),   # your vendor
])
def test_provider_routing_correct(monkeypatch, model, expected_host):
    ep = _reload_endpoints(monkeypatch, {})
    assert expected_host in ep.lookup(model).base_url
```

Run `make test` to verify no regressions were introduced.

---

## FAQ

**Q: Does the vendor require a prepaid balance?**
A: Yes. muselab does not manage billing. Only Pro OAuth draws from the
subscription's included quota.

**Q: Can one session switch vendors mid-conversation?**
A: No. If the current session already has messages, switching the model
prompts a confirmation and forks a new session that uses the chosen model;
the original is kept in history. Empty sessions switch in place. This
avoids cross-vendor thinking-signature drift and inaccessible `tool_use`
context — see the "Switching model mid-conversation" section in
[providers.md](providers.md).

**Q: Are `prefix` and `models` redundant in the catalog?**
A: `prefix` is used by the dispatcher for routing; `models` is the explicit
list shown in the UI dropdown. Every value in `models` must begin with
`prefix`.

**Q: Is a restart required after editing the catalog?**
A: Yes. The catalog is a Python module-level constant; hot reload is a
separate engineering concern.

**Q: Smart routing between vendors (e.g. plan tasks → Sonnet, code → DeepSeek)?**
A: This should not be implemented inside muselab.
[claude-code-router](https://github.com/musistudio/claude-code-router) is
the appropriate tool for this. muselab's design philosophy is "thin layer;
the user selects the model".
