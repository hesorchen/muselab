# Codex Gateway

> [简体中文](codex-gateway_zh.md)

muselab supports Codex-backed models through a **local Anthropic-compatible
gateway**. The gateway is a sidecar process: muselab still talks to the Claude
Agent SDK and an Anthropic Messages API shape; the sidecar translates that
request to the user's own Codex/OpenAI backend and translates the response back.

muselab does **not** store Codex OAuth credentials and does **not** call
OpenAI-native APIs directly.

```text
muselab → Claude Agent SDK → Anthropic Messages request
        → Codex Gateway on 127.0.0.1
        → user-authenticated Codex/OpenAI backend
```

## What is built in

The model catalog includes a disabled-by-default provider preset:

| Field | Default |
|---|---|
| Provider | `Codex Gateway` |
| Endpoint | `http://127.0.0.1:8766/anthropic` |
| Env key | `CODEX_GATEWAY_API_KEY` |
| Base URL override | `CODEX_GATEWAY_BASE_URL` |
| Internal prefix | `codex:` |
| Models | `codex:gpt-5-codex`, `codex:gpt-5`, `codex:gpt-5-mini` |

The `codex:` prefix is muselab-internal. Before sending the model id to the
gateway, muselab strips the prefix, so `codex:gpt-5-codex` becomes
`gpt-5-codex` on the gateway side.

## Enable it

1. Run your Codex gateway locally and bind it to loopback only:

   ```bash
   # Example only — use the gateway implementation you trust.
   codex-gateway --host 127.0.0.1 --port 8766
   ```

2. Put a strong gateway token in `.env`:

   ```bash
   CODEX_GATEWAY_API_KEY=replace-with-a-random-local-token
   # Optional if your gateway uses a different loopback port:
   # CODEX_GATEWAY_BASE_URL=http://127.0.0.1:8766/anthropic
   ```

3. Restart muselab if you edited `.env` by hand, or paste the key in
   **Settings → Providers → Codex Gateway** to apply it without restart.

4. Pick a `codex:*` model in the chat model dropdown.

## Gateway requirements

The sidecar must implement enough of the Anthropic Messages API for agent use:

- `POST /v1/messages` or the equivalent path under the configured base URL;
- text streaming in the Anthropic SSE event shape;
- `tool_use` and `tool_result` round trips;
- Anthropic-style error responses for auth, quota, invalid model, and network
  failures;
- support for the headers muselab sends: `x-api-key` and/or
  `Authorization: Bearer`.

If plain chat works but tools fail, the gateway is chat-only and should not be
advertised as full muselab agent support.

## Context window notes

muselab's context meter treats the built-in Codex Gateway models as 400K-context
models, matching OpenAI's public GPT-5 / GPT-5 mini / GPT-5-Codex model cards
(128K max output; GPT-5-Codex is Responses-API-only behind the gateway).

A gateway can still fail earlier with `input exceeds the context window` if its
translation layer, selected backend model, or account tier has a smaller
effective window. In that case, start a fresh session, compact the conversation,
or switch to a model/gateway path with a larger confirmed window.

## Security model

- Keep the gateway on `127.0.0.1` by default.
- Require a token even on loopback.
- Do not log `Authorization`, `x-api-key`, OAuth access tokens, refresh tokens,
  cookies, or raw Codex auth files.
- Do not commit gateway runtime state. `.env`, `.codex/`, `.cli-proxy-muselab/`,
  `.muselab/codex-gateway/`, logs, and provider overrides are local-only.
- If you expose the gateway beyond localhost, put HTTPS and a reverse proxy in
  front and use a high-entropy token.

## Why not native OpenAI/Codex support?

muselab's invariant is that the app has one agent runtime: the Claude Agent SDK.
That runtime owns tool execution, MCP, skills, permissions, streaming, and
transcripts. Native OpenAI/Codex APIs have different message, streaming, tool,
and error shapes. Supporting them directly would require a second agent runtime
inside muselab. The gateway boundary keeps muselab small while still allowing
Codex-backed models when a compatible adapter is available.
