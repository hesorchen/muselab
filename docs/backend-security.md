# Backend security model

> [中文](backend-security_zh.md)

muselab is a single-user application. One shared token protects the UI and API, but it is not a fine-grained authorization system: anyone holding it should be treated as having every capability available to the muselab service user.

## Authentication

- `MUSELAB_TOKEN` must contain at least 16 characters; startup fails when it is missing or too short.
- Normal API calls prefer the `X-Auth-Token` header.
- A small set of browser-navigation resources accept `?token=`, including downloads, raw previews, and persisted attachments.
- Token checks use constant-time comparison.
- `GET /api/health` and rate-limited browser error reporting are public; business APIs require authentication by default.

Query parameters can enter browser history and proxy logs. muselab filters tokens from its own access log, but the reverse proxy must also redact them. Any non-local deployment requires HTTPS.

## One-time SSE ticket

Sending a message uses two requests:

1. `POST /api/chat/stream/start` authenticates with a header and carries the prompt, session, model, permission, attachment IDs, and mobile flag in JSON.
2. The backend returns a single-use ticket valid for 60 seconds; the browser connects to `GET /api/chat/stream?ticket=...`.

The ticket is removed from memory on first redemption, keeping the prompt and long-lived token out of the SSE URL. The legacy `GET /stream?token=...` query form remains only for old-client compatibility and should not be used by new integrations.

## Terminal WebSocket ticket

Real terminals use a separate two-step exchange:

1. An authenticated request calls `POST /api/terminals/{terminal_id}/ticket`.
2. The backend returns a single-use ticket valid for 30 seconds. The browser presents both `muselab-terminal-v1` and the ticket as WebSocket subprotocols.

Only a SHA-256 digest of the ticket is stored. Redemption is bound to the terminal ID; when a WebSocket `Origin` is present, its host must match the request `Host`. The long-lived token is never placed in the WebSocket URL.

## Authority boundaries

### Files API

Every Files API request is bound to the default or a registered workspace. Paths are normalized and must remain inside the selected root; symlink targets must also stay inside it. Sensitive names, NUL bytes, and direct writes into the dustbin are rejected.

Workspace registration is not service-user isolation. Register only directories you intend to expose through the UI.

### Agent and terminal

The Files API path boundary does **not** constrain Agent tools or real terminals:

- Agent Bash, Read, Write, and related tools run according to the selected SDK permission mode.
- Preview terminals are real Unix PTYs and can access paths outside the workspace with the muselab service user's OS authority.
- Terminal processes receive a minimal environment and do not inherit `MUSELAB_TOKEN` or provider API keys. This reduces credential exposure but does not restrict filesystem permissions.

Set `MUSELAB_TERMINAL_ENABLED=0` when terminals are not needed. On production or shared machines, run muselab as a dedicated unprivileged user and use containers, VMs, or OS permissions for real isolation.

## Provider credential isolation

CLI subprocesses for non-Anthropic providers receive a minimal full environment replacement, not a merge with the parent environment. It contains only process, proxy, and TLS variables plus the selected provider's endpoint and credentials.

`CLAUDE_CONFIG_DIR` points to a per-OS-user temporary directory and any Claude OAuth credential found there is removed. This prevents third-party requests from silently falling back to Anthropic. Only `~/.claude/skills/` is mapped into the isolated root so user Skills remain available; settings, hooks, plugins, credentials, and other provider keys stay isolated. `MUSELAB_TOKEN` is not passed to that subprocess.

## Settings writes

The settings API writes only explicitly allowlisted fields. Deployment-level values such as `MUSELAB_TOKEN`, `MUSELAB_ROOT`, and `PATH` cannot be changed through the UI.

- API keys are masked in responses.
- A value containing the mask marker cannot overwrite a real key.
- CR and LF are stripped to prevent `.env` line injection.
- `.env` uses a temporary file and atomic replacement; hot-reloadable values are also updated in the current process.

## HTTP defenses

Every response sets:

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: SAMEORIGIN`
- `Referrer-Policy: same-origin`

Raw HTML and SVG previews use isolated responses with a strict CSP. The main UI does not have a global strict CSP because it uses Alpine inline directives.

## Threat model and deployment guidance

| Risk | Effective impact | Mitigation |
|---|---|---|
| Token disclosure | Sessions, workspaces, Agent, settings, and real terminals become accessible, effectively granting remote service-user operations | Use a long random token, HTTPS, proxy-log redaction, and a dedicated service user |
| Malicious preview | HTML or SVG can contain active content | Keep preview CSP and isolation headers enabled |
| Malicious attachment or page | Prompt injection can steer the Agent | Use stricter permissions for external content and do not auto-approve high-risk actions |
| Upgrade endpoint | An authenticated user can trigger install scripts for fixed packages | Block the endpoint at the reverse proxy if online upgrade is not needed |
| Multiple workers | In-memory tickets, rate limits, active turns, and terminal registries are not shared | Use the default single-worker deployment |

See the repository-root `SECURITY.md` for deployment details.
