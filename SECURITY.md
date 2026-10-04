# Security Policy

## Threat model

muselab is a single-user, self-hosted web application. Whoever holds the
`MUSELAB_TOKEN` can:

- Read, write, upload, and delete files under `MUSELAB_ROOT` and every directory registered in the workspace picker
- Drive a Claude Agent SDK session running with `permission_mode="bypassPermissions"` and the selected workspace as `cwd`

This is intentional — muselab is a local Agent workbench, not a sandbox. The
practical implication is that **a leaked token is equivalent to remote code
execution with the service user's OS permissions**, not merely file-API access
inside a workspace. Operate accordingly:

- Run the service as a dedicated unprivileged user (not `root`, not your login account)
- Point `MUSELAB_ROOT` at a dedicated directory you own, and register only intended workspace directories (broad system roots such as `/`, `/etc`, `/root`, `/home`, `/var`, `/usr`, `/boot` are refused)
- Place it behind nginx or Caddy with HTTPS; never expose port `8765` directly to the public internet
- Treat `MUSELAB_TOKEN` like a password: keep it long, random, and rotate it if a leak is suspected
- Add HTTP basic auth as a second factor in front of muselab when exposed beyond your LAN

## What muselab defends against

- `..` traversal and workspace-relative symlinks to locations outside all registered workspaces in file APIs. Actual symlinks may point into another registered workspace; internal-state and sensitive-file exclusions still apply.
- Reading or overwriting credential-shaped files (`.env*`, SSH private keys, `*.pem`, `credentials.json`, etc.) — blocked even with a valid token
- Same-origin script access from previewed `.html` / `.svg` / Markdown — raw HTML/SVG use an opaque-origin sandbox and scoped preview tickets, and Markdown passes through DOMPurify before insertion. If the sanitizer is unavailable, the app displays escaped plain text. Previewed scripts may still request external HTTPS resources; the sandbox is not a network-isolation boundary.
- Token length below 16 characters or `MUSELAB_ROOT` pointing at system paths — refused at startup
- Timing side-channel on token comparison — constant-time comparison via `hmac.compare_digest`
- Default response headers: `X-Content-Type-Options: nosniff` (no MIME sniffing of file previews) · `Referrer-Policy: same-origin` (tokens in query strings do not leak via cross-origin `Referer`) · `X-Frame-Options: SAMEORIGIN` (external sites cannot iframe the UI)
- `noindex, nofollow, noarchive` meta tags and `/robots.txt` discourage cooperating search crawlers. They do not enforce access control or prevent discovery of an exposed service.

## Reverse-proxy logging caveat

First-party browser requests use the following credentials:

| Surface | Credential | Boundary |
|---|---|---|
| Normal API fetch | `X-Auth-Token` header | Full single-user service authority |
| Chat SSE / multiplex stream | Header-authenticated mint, then a single-use stream ticket | Expiring stream/subscription parameters |
| Terminal WebSocket | Single-use ticket in the WebSocket subprotocol | One terminal/workspace; 30-second admission window |
| HTML, SVG, images, PDF and relative Markdown images | Preview ticket | One resolved file and registered workspace; reusable for browser range/conditional requests during its TTL |
| File downloads and chat exports | Single-use resource ticket | Exact file/workspace or exact session export |

Preview tickets default to 600 seconds and can be configured from 30 to 3600
seconds with `MUSELAB_PREVIEW_TICKET_TTL_S`. They are held in process memory,
not localStorage. They authorize that path during the TTL, including updates
to the same file; they do not grant general API or other-file access.

First-party file resource URLs do not contain the long-lived global token.
Legacy query-token routes remain for older clients; the SSE compatibility
fallback can also emit a query token when connected to an older server that
lacks stream-ticket endpoints. A legacy `/api/files/raw?token=…`
request redirects to a scoped ticket before serving the document, so even a
script-capable SVG opened in a new tab cannot read the global token from its
own URL. The **initial legacy request can still appear in a proxy log or
browser history**; a redirect cannot erase logs already written.

MuseLab's own access-log filter removes sensitive query values, but proxies
and CDNs have independent logs. Redact both `token` and `ticket`, or omit
query strings entirely. Treat unexpired resource URLs as limited bearer links.

Examples:

```nginx
# nginx: redact token= in the access log
log_format muselab_safe '$remote_addr - $remote_user [$time_local] '
                        '"$request_method $uri?<redacted> $server_protocol" '
                        '$status $body_bytes_sent';
access_log /var/log/nginx/muselab.log muselab_safe;
```

```caddy
# Caddy: drop the query string from access logs
log {
    output file /var/log/caddy/muselab.log
    format filter {
        request>uri query {
            delete token
            delete ticket
        }
    }
}
```

## Bundled frontend dependencies

`frontend/vendor/manifest.json` records exact shipped frontend versions, file
hashes, sources and license material. Run `python scripts/check-vendor.py` to
check the inventory and `python scripts/check-vendor.py --audit` to check the
listed npm versions against OSV. The scan prints reviewed version matches
where the affected implementation is absent from the exact hash-bound bundle,
and identifies upstream fragments whose npm version cannot be established.
See `scripts/vendor/README.md` for those precise limits. Python `pip-audit` covers a different graph;
neither check alone audits the OS image or proves absence of vulnerabilities.

## What muselab does NOT defend against

- A compromised `MUSELAB_TOKEN` — full access is granted by design
- Tool or shell access beyond a registered workspace — `bypassPermissions` intentionally gives the agent the service user's authority; file-API containment is not an OS sandbox
- Resource exhaustion at the request layer — upload size is capped (100 MB by default, configurable via `MUSELAB_MAX_UPLOAD_MB`); `/api/files/grep` has a soft 8-second time budget and a 1 MB per-file cap; `/api/log/client-error` is limited to an 8 KiB streaming body and 30 requests per IP per minute. The generated Caddy configuration also rejects oversized bodies for that unauthenticated route. Other endpoints do not have per-IP rate limiting. If muselab is exposed to more than one trusted user, place a reverse proxy (Caddy or nginx) in front with global rate limits.

## Reporting a vulnerability

Email **hesorchen@gmail.com** with the subject line `muselab security`. Please
do not open a public issue for anything that could be used to read other users'
files or steal tokens. Expect a response within 7 days.
