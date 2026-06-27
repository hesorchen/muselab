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
| Endpoint | `http://127.0.0.1:8317` |
| Env key | `CODEX_GATEWAY_API_KEY` |
| Base URL override | `CODEX_GATEWAY_BASE_URL` |
| Internal prefix | `codex:` |
| Models | `codex:gpt-5.5`, `codex:gpt-5.4`, `codex:gpt-5.4-mini`, `codex:gpt-5.3-codex-spark` |

The `codex:` prefix is muselab-internal. Before sending the model id to the
gateway, muselab strips the prefix, so `codex:gpt-5.5` becomes `gpt-5.5` on
the gateway side.

## Enable it

1. Start from muselab's recommended CLIProxyAPI config:

   ```bash
   mkdir -p ~/.cli-proxy-muselab
   cp examples/cli-proxy-muselab.config.yaml ~/.cli-proxy-muselab/config.yaml
   ```

2. Edit `~/.cli-proxy-muselab/config.yaml`:

   - replace `replace-with-a-random-local-token` with a strong local token;
   - keep `disable-cooling: true` and `session-affinity: false` unless you
     explicitly want the proxy to add local cooldown windows.

3. Run CLIProxyAPI locally and bind it to loopback only:

   ```bash
   cli-proxy-api -config ~/.cli-proxy-muselab/config.yaml
   ```

4. Put the same gateway token in `.env`:

   ```bash
   CODEX_GATEWAY_API_KEY=replace-with-a-random-local-token
   # Optional if your gateway uses a different loopback port:
   # CODEX_GATEWAY_BASE_URL=http://127.0.0.1:8317
   ```

5. Restart muselab if you edited `.env` by hand, or paste the key in
   **Settings → Providers → Codex Gateway** to apply it without restart.

6. Pick a `codex:*` model in the chat model dropdown.

The recommended CLIProxyAPI template disables the proxy's local auth/model
cooldown scheduling. This makes muselab avoid extra proxy-side blackout windows
after an upstream failure, which is closer to the direct Codex app/CLI
experience. It does not bypass real upstream quota or model-level 429s.

## Reference implementation: CLIProxyAPI sidecar

muselab's reference setup runs **CLIProxyAPI** next to muselab as a local
sidecar:

```text
browser
  → muselab backend
  → Claude Agent SDK
  → Anthropic Messages API request (model: codex:gpt-5.5)
  → muselab strips the codex: prefix (model: gpt-5.5)
  → http://127.0.0.1:8317/v1/messages
  → CLIProxyAPI
  → user-authenticated Codex backend
```

The boundary is:

- **muselab owns** the provider catalog, model picker, session-level base URL /
  API key injection, and the agent loop / tool calls / transcripts through the
  Claude Agent SDK.
- **CLIProxyAPI owns** Codex-side authentication, translating Anthropic Messages
  requests to the Codex/OpenAI backend, and translating streaming responses and
  errors back to the Anthropic shape.
- **The user owns** running the sidecar locally and putting the same local token
  in both `~/.cli-proxy-muselab/config.yaml` and muselab's
  `CODEX_GATEWAY_API_KEY`.

`examples/cli-proxy-muselab.config.yaml` is muselab's recommended minimal
reference config. It intentionally uses these defaults:

| Setting | Recommended value | Why |
|---|---|---|
| `host` | `127.0.0.1` | Keep the gateway local-only and avoid exposing local Codex access to the internet |
| `port` | `8317` | Matches muselab's built-in `CODEX_GATEWAY_BASE_URL` default |
| `api-keys` | user-generated strong token | Prevent other local processes from calling the gateway unauthenticated |
| `disable-cooling` | `true` | Avoid extra proxy-side local cooldown blackout windows |
| `session-affinity` | `false` | Do not bind muselab sessions to a specific credential by default |
| `logging-to-file` | `false` | Reduce the risk of writing prompts, tokens, or upstream errors to disk |
| `remote-management.allow-remote` | `false` | Disable the remote management surface |

muselab does **not** install or start this sidecar automatically. If you want it
to start on boot, manage `cli-proxy-api -config ~/.cli-proxy-muselab/config.yaml`
with systemd, launchd, or another local supervisor. Do not commit Codex OAuth
files or gateway logs to the repository.

### Docker note

If muselab runs in Docker, `http://127.0.0.1:8317` means **inside the muselab
container**, not the host machine. Options:

- run the gateway in the same compose/network and set `CODEX_GATEWAY_BASE_URL`
  to that gateway service name;
- or point the container at a host-running gateway, for example with
  `host.docker.internal` (Linux may also need extra host-gateway configuration).

Do not bind the gateway to `0.0.0.0` and expose it directly to the internet. If
you must access it across machines, put it behind HTTPS, a reverse proxy, and a
firewall, and use a high-entropy token.

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
models. The gateway can still enforce a different effective window depending on
the selected backend model and account tier.

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
