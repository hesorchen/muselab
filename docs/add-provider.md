# Adding a new LLM provider

> [简体中文](add-provider_zh.md)

muselab is not locked to Claude. Any vendor that exposes an
**Anthropic Messages API-compatible endpoint** can be integrated.
**The recommended path is to add a provider from the Settings UI — no code
edits, no restart.** All Claude SDK capabilities
(Read/Edit/Bash/Grep/MCP/Skills/CLAUDE.md auto-load) work across vendors
automatically.

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
| Codex Gateway | `http://127.0.0.1:8317` | ✅ built-in local-gateway preset |

**Vendors without an Anthropic endpoint** are not supported. Options are to
request that the vendor ship a compatible endpoint, or to use
[claude-code-router](https://github.com/musistudio/claude-code-router) as a
protocol translator (lossy; requires an additional process).

For Codex/OpenAI-backed models, the built-in **Codex Gateway** preset assumes a
user-run local sidecar that already speaks Anthropic Messages at
`http://127.0.0.1:8317`. muselab does not read Codex OAuth files and
does not call OpenAI-native APIs directly. See [codex-gateway.md](codex-gateway.md).

---

## Two ways to add a provider

Pick based on your goal:

- **Path A (recommended, for users)** — add the provider from the Settings
  UI. Takes effect immediately; no code edits, no restart.
- **Path B (for contributors)** — add a built-in default in
  `backend/endpoints.py` so every user gets it out of the box. Use this when
  opening a PR.

### Path A: add it in Settings (recommended)

1. Open **Settings → Providers** and click **New provider**.
2. Fill in four fields:
   - **Endpoint** — the vendor's Anthropic-compatible endpoint (e.g.
     `https://api.acme.com/anthropic`)
   - **Prefix** — the model-name prefix (e.g. `acme-`); the dispatcher routes on it
   - **Models** — the list of model ids, each beginning with the prefix
     (e.g. `acme-large`, `acme-small`)
   - **API key** — fill it here, or add it separately later
3. Save. The **model dropdown shows the new group immediately** — no restart.

The metadata is written to `provider_overrides.json` at the project root
(next to `mcp.json`); the API key goes to `.env` and refreshes `os.environ`
in-process. Both are picked up automatically on the next start. The backend
mints the env-key name for you, so you never touch it.

> Editing a built-in provider (endpoint / model list) or removing one happens
> in the same place; a deleted built-in can be brought back with
> **Restore default**.

### Path B: add a built-in default (for contributors)

To ship a provider as a muselab built-in (available to everyone out of the
box), append an entry to `CATALOG`:

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

`CATALOG` is a Python module-level constant, so a **restart is required** for
the change to take effect:

```bash
# Docker
docker compose restart

# Or native: kill the old uvicorn, then uv run uvicorn ...
```

Then add the API key to `.env` (or paste it in Settings), and the new group
appears in the model dropdown.

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

When shipping a built-in provider via Path B (a PR), add a corresponding test in `tests/test_endpoints.py`:

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

**Q: Is a restart required after adding or editing a provider?**
A: It depends on the path. **Changes made in Settings take effect
immediately — no restart**: overrides live in `provider_overrides.json` and
are hot-reloaded on file change, and API keys are written to `.env` and
refresh `os.environ` in-process. A restart is only needed when you **edit
`backend/endpoints.py` directly**, because `CATALOG` is a Python
module-level constant.

**Q: Smart routing between vendors (e.g. plan tasks → Sonnet, code → DeepSeek)?**
A: This should not be implemented inside muselab.
[claude-code-router](https://github.com/musistudio/claude-code-router) is
the appropriate tool for this. muselab's design philosophy is "thin layer;
the user selects the model".
