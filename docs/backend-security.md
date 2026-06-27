# Security Model

> [简体中文](backend-security_zh.md)

This page describes muselab's security architecture: how authentication tokens flow through the system, what the filesystem containment layer does, which settings can be changed at runtime and which cannot, how third-party model providers are isolated from your Anthropic account, and the network posture defaults. For the vulnerability-reporting policy and operator hardening checklist, see [../SECURITY.md](../SECURITY.md).

---

## Threat model

muselab is a **single-user, self-hosted, localhost-first** application. There are no user accounts, no role-based access control, and no multi-tenancy. Whoever holds `MUSELAB_TOKEN` can read, write, upload, and delete any file under `MUSELAB_ROOT`, and can drive a Claude Agent SDK session running with `permission_mode="bypassPermissions"` and `cwd=MUSELAB_ROOT`. The practical implication is that a leaked token is equivalent to remote shell access scoped to the archive directory. The design deliberately accepts this: muselab is an AI archive manager, not a sandbox. The mitigations are operational — run as a dedicated unprivileged user, keep the token long and random, put a TLS reverse proxy in front, never expose port 8765 to the public internet.

---

## Authentication

### Token source and minimum length

`MUSELAB_TOKEN` is read from the environment (with a fallback to the deprecated `PORTAL_TOKEN`) at module import time in [`backend/settings.py:L195`](../backend/settings.py#L195). A minimum length of **16 characters** is enforced at startup: if the token is absent or shorter than 16 chars, the server raises `RuntimeError` and refuses to start ([`backend/settings.py:L229-L235`](../backend/settings.py#L229-L235)).

### Constant-time comparison

All token checks use [`hmac.compare_digest()`](../backend/auth.py#L7-L19) rather than Python's `==` operator. String equality in Python short-circuits at the first mismatched byte, leaking matched-prefix length via response timing over a LAN. `hmac.compare_digest` runs in time proportional to the longer of the two inputs regardless of where they diverge ([`backend/auth.py:L7-L19`](../backend/auth.py#L7-L19)).

### Three dependency variants

Three FastAPI dependencies handle different transport constraints ([`backend/auth.py:L22-L54`](../backend/auth.py#L22-L54)):

| Dependency | Used by | Token accepted from |
|---|---|---|
| `require_token` | Most `/api/files/*`, `/api/settings/*`, `/api/meta`, `/api/presence` | `X-Auth-Token` header only |
| `require_token_query` | `/api/files/raw`, `/api/files/download` | `?token=` query param only |
| `require_token_header_or_query` | Chat SSE stream (`/api/chat/stream`) | Either header or `?token=` |

The query-param variants exist because browsers cannot send custom headers for `<iframe src>` and `EventSource` connections. Image previews that need authorization are fetched with `X-Auth-Token` and rendered as blob URLs, so the global token is not placed in image URLs. `fetch()` calls can use headers and do so in preference to the query param; the query-param path remains as a fallback for iframe downloads and copied links ([`backend/auth.py:L33-L54`](../backend/auth.py#L33-L54)).

### Unauthenticated routes

The following routes require no token:

| Route | Reason |
|---|---|
| `GET /api/health` | Liveness probe for Docker / k8s / Caddy `health_uri` ([`backend/main.py:L529-L535`](../backend/main.py#L529-L535)) |
| `POST /api/log/client-error` | Browser error capture before auth is established; rate-limited to 30/min per IP ([`backend/main.py:L582-L622`](../backend/main.py#L582-L622)) |
| `GET /`, `/static/*`, `/sw.js`, `/robots.txt`, `/static/assets/manifest.webmanifest` | HTML shell, frontend assets, PWA manifest — no private data |

---

## Filesystem containment

Every file operation in muselab's files API passes through `safe_resolve()` before any filesystem call. This function blocks path traversal (`../../etc/passwd`), symlink escapes (by resolving the target path and checking it falls inside `ROOT`), NUL-byte injection, and access to credential-shaped filenames (`.env*`, `id_rsa`, `*.pem`, `credentials.json`, and 30+ other patterns) — even with a valid token. `MUSELAB_ROOT` itself is forbidden from pointing at system paths (`/`, `/etc`, `/root`, `/home`, `/var`, `/usr`, `/boot`), enforced at startup.

Full details, including the complete `SENSITIVE_NAMES` and `SENSITIVE_SUFFIX` blocklists and the `allow_sensitive=True` exceptions for trash restore, are documented in [backend-files.md](backend-files.md) (see the `safe_resolve` section).

---

## Settings surface

### What `PUT /api/settings` can change

The settings write endpoint ([`backend/api_settings.py:L275-L404`](../backend/api_settings.py#L275-L404)) accepts a strictly typed `SettingsIn` body and applies a **name whitelist** before touching `.env`. Writable fields are:

| Field | Env var(s) written |
|---|---|
| `anthropic_api_key` / `deepseek_api_key` / `zhipuai_api_key` / `minimax_api_key` | Corresponding `*_API_KEY` vars |
| `provider_keys` (dict) | Any name in the provider catalog's `env_key` set, or matching `^MUSELAB_PROVIDER_[A-Z0-9_]+_API_KEY$` |
| `default_model` | `MUSELAB_DEFAULT_MODEL` + `MUSELAB_MODEL` (kept in sync) |
| `default_permission` | `MUSELAB_DEFAULT_PERMISSION` |
| `provider_disabled` | `MUSELAB_DISABLED_PROVIDERS` |

Names that are not in the whitelist are **silently dropped** — `PATH`, `MUSELAB_TOKEN`, `MUSELAB_ROOT`, and any arbitrary env var cannot be written through this endpoint ([`backend/api_settings.py:L309-L311`](../backend/api_settings.py#L309-L311)).

### What can never be changed via API

- `MUSELAB_TOKEN` — not in any whitelist; changing the auth token from within an authed session would be a privilege-escalation surface.
- `MUSELAB_ROOT` — changing the root directory at runtime could silently redirect file operations.
- `PATH` or any other process environment variable not in the whitelist.

### Masked-value rejection

`GET /api/settings` returns provider API keys masked (`first4•••last4`, using U+2022 BULLET). Any value containing `•` submitted to `PUT /api/settings` is **rejected without writing**, preventing a frontend bug from accidentally round-tripping masked display values back over real keys ([`backend/api_settings.py:L319-L324`](../backend/api_settings.py#L319-L324)).

### Atomic `.env` rewrite

Settings changes are written atomically via `tempfile.mkstemp` + `os.replace` ([`backend/api_settings.py:L163-L173`](../backend/api_settings.py#L163-L173)). CR/LF characters are stripped from values before writing to prevent newline-injection attacks that could split a value into extra `KEY=VALUE` lines on the next `load_dotenv` ([`backend/api_settings.py:L129-L133`](../backend/api_settings.py#L129-L133)). `os.environ` is updated in-process immediately after the file write so changes take effect without a restart.

---

## Billing isolation for third-party models

When muselab routes a session to a third-party provider (DeepSeek, GLM, MiniMax, Kimi, Qwen, MiMo, Qianfan), it builds a **minimal allowlisted environment** and passes it to the Claude CLI subprocess as a **full replacement** — not a merge — of the process environment ([`backend/endpoints.py:L851-L930`](../backend/endpoints.py#L851-L930)).

The substitution sets exactly:

```
ANTHROPIC_BASE_URL       = <vendor endpoint>
ANTHROPIC_API_KEY        = <vendor key>      # x-api-key header
ANTHROPIC_AUTH_TOKEN     = <vendor key>      # belt-and-suspenders Bearer
CLAUDE_CODE_OAUTH_TOKEN  = ""                # kills OAuth fallback
CLAUDE_OAUTH_TOKEN       = ""                # kills OAuth fallback
CLAUDE_CONFIG_DIR        = <isolated tmp dir>
```

Plus a short allowlist of process basics (`PATH`, `HOME`, `USER`, locale, proxy, TLS-CA vars) — nothing else.

**Why `CLAUDE_CONFIG_DIR` isolation prevents silent Anthropic billing.** The Claude CLI prefers `~/.claude/.credentials.json` (Pro OAuth) over `ANTHROPIC_API_KEY`. Without isolation, a DeepSeek session would send the Claude OAuth token to `api.deepseek.com`, get a 401 from DeepSeek, and then silently fall back to `api.anthropic.com` — billing your Claude Pro account. Pointing `CLAUDE_CONFIG_DIR` at a per-user temp directory (`/tmp/muselab-vendor-cli-config-<uid>/`) that contains no credentials file forces the CLI to use the injected API key. Any leaked credentials file in that directory is deleted on each call ([`backend/endpoints.py:L879-L887`](../backend/endpoints.py#L879-L887)).

**Why the minimal allowlist prevents key exfiltration.** The CLI subprocess runs `bypassPermissions` and is internet-capable. Inheriting the full environment would expose `MUSELAB_TOKEN` and every other provider's `*_API_KEY` to an agent that could exfiltrate them via a Bash tool call (`echo $MUSELAB_TOKEN`). The allowlist ensures the subprocess only sees what it needs to connect to the vendor ([`backend/endpoints.py:L895-L910`](../backend/endpoints.py#L895-L910)).

See also [routing.md](routing.md) for provider catalog and model-resolution details.

---

## Network posture

**Binding address.** muselab binds to `127.0.0.1` (loopback only) by default. The comment in [`backend/settings.py:L206-L209`](../backend/settings.py#L206-L209) explicitly notes that LAN binding would be a footgun for the default single-user install. Override to `0.0.0.0` in `.env` (`MUSELAB_HOST`) only for LAN / VPS / Docker scenarios where you have a TLS terminator in front.

**Response headers.** The `_security_headers` middleware ([`backend/main.py:L299-L331`](../backend/main.py#L299-L331)) attaches three headers to every response via `setdefault` (endpoint-specific headers can override):

| Header | Value | Purpose |
|---|---|---|
| `X-Content-Type-Options` | `nosniff` | Prevents MIME sniffing of file previews |
| `Referrer-Policy` | `same-origin` | Prevents token leakage via `Referer` on cross-origin navigation |
| `X-Frame-Options` | `SAMEORIGIN` | Blocks external-site framing of the UI |

**No global CSP.** The UI uses Alpine.js inline directives (`x-on:`, `@click`, `:class`) and multiple inline `<script>` tags. A strict CSP would require per-request nonces or eval allowances; the maintenance cost is not justified for a single-user app. HTML/SVG files served via `/api/files/raw` *do* get a per-response strict CSP ([`backend/files.py:L694-L704`](../backend/files.py#L694-L704)).

**No built-in HSTS.** `Strict-Transport-Security` is only meaningful over HTTPS. muselab normally runs at `127.0.0.1` without TLS; HSTS on plaintext localhost would confuse reverse-proxy setups. Operators should set HSTS at the reverse proxy layer.

**Reverse-proxy log caveat.** muselab's own access logger strips `token=` from URLs via `_TokenFilter` ([`backend/main.py:L23-L62`](../backend/main.py#L23-L62)), but a reverse proxy records the raw URL. Configure your proxy to redact the `token` query parameter — examples for nginx and Caddy are in [../SECURITY.md](../SECURITY.md).

---

## Known limitations

| Limitation | Impact | Mitigation |
|---|---|---|
| **Single-user, no RBAC** | Token possession grants full archive access; no per-user or per-directory scoping | Run for one trusted user only; treat token as a root credential |
| **No per-request rate limiting** (most endpoints) | A valid token can flood the server; upload size is capped (100 MB) but request rate is not | Place Caddy or nginx in front with global rate limits if exposed to more than one user ([SECURITY.md](../SECURITY.md)) |
| **Upgrade endpoint is token-gated RCE by design** | `POST /api/settings/upgrade` runs `uv` and `npm` subprocesses; package installs run arbitrary scripts ([`backend/api_settings.py:L1367-L1388`](../backend/api_settings.py#L1367-L1388)) | Token already grants equivalent access; package names are fixed literals, not user-supplied. Block `POST /api/settings/upgrade` at reverse proxy if you want to remove the surface |
| **No multi-worker support** | Rate-limit buckets (`_CLIENT_ERR_BUCKETS`) are in-process only; a multi-worker deployment would silently skip limits ([`backend/main.py:L554-L556`](../backend/main.py#L554-L556)) | Use single-worker deployment (default) |
| **Token in reverse-proxy logs** | SSE and download endpoints use `?token=` query params; muselab strips them locally but upstream proxies record the raw URL | Configure proxy log format to redact `token` — see [../SECURITY.md](../SECURITY.md) for nginx and Caddy examples |

---

*Related pages: [configuration.md](configuration.md) · [routing.md](routing.md) · [backend-files.md](backend-files.md) · [../SECURITY.md](../SECURITY.md)*
