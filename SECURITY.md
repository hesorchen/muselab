# Security Policy

## Threat model

muselab is a single-user, self-hosted web application. Whoever holds the
`MUSELAB_TOKEN` can:

- Read, write, upload, and delete files under `MUSELAB_ROOT`
- Drive a Claude Agent SDK session running with `permission_mode="bypassPermissions"` and `cwd=MUSELAB_ROOT`

This is intentional — muselab is an AI archive manager, not a sandbox. The
practical implication is that **a leaked token is equivalent to remote code
execution within `MUSELAB_ROOT`**. Operate accordingly:

- Run the service as a dedicated unprivileged user (not `root`, not your login account)
- Point `MUSELAB_ROOT` at `$HOME` or a subdirectory you own (system paths such as `/`, `/etc`, `/root`, `/home`, `/var`, `/usr`, `/boot` are refused at startup)
- Place it behind nginx or Caddy with HTTPS; never expose port `8765` directly to the public internet
- Treat `MUSELAB_TOKEN` like a password: keep it long, random, and rotate it if a leak is suspected
- Add HTTP basic auth as a second factor in front of muselab when exposed beyond your LAN

## What muselab defends against

- Path traversal outside `MUSELAB_ROOT`
- Reading or overwriting credential-shaped files (`.env*`, SSH private keys, `*.pem`, `credentials.json`, etc.) — blocked even with a valid token
- Same-origin XSS via uploaded `.html` / `.svg` / Markdown — `/api/files/raw` serves arbitrary types as `application/octet-stream` attachments, HTML/SVG are served with a strict CSP and sandbox, and rendered Markdown is run through DOMPurify before insertion
- Token length below 16 characters or `MUSELAB_ROOT` pointing at system paths — refused at startup
- Timing side-channel on token comparison — constant-time comparison via `hmac.compare_digest`
- Default response headers: `X-Content-Type-Options: nosniff` (no MIME sniffing of file previews) · `Referrer-Policy: same-origin` (tokens in query strings do not leak via cross-origin `Referer`) · `X-Frame-Options: SAMEORIGIN` (external sites cannot iframe the UI)
- `noindex, nofollow, noarchive` meta tags and `/robots.txt` — accidental public exposure will not result in crawling

## Reverse-proxy logging caveat

The streaming chat endpoint uses Server-Sent Events (`EventSource`), which the
browser spec forbids from sending custom headers. The auth token therefore
travels as a query string parameter (`?token=…`) on that one endpoint only.
muselab's own access log strips it before writing (`_TokenFilter` in
`backend/main.py`), but a reverse proxy in front of muselab will record the
raw URL by default. **If you put muselab behind nginx / Caddy / a CDN,
configure access logs to strip or mask the `token` query parameter.**
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
        }
    }
}
```

## What muselab does NOT defend against

- A compromised `MUSELAB_TOKEN` — full access is granted by design
- Symlink escapes from within `MUSELAB_ROOT` — do not place attacker-controlled symlinks in the archive
- Resource exhaustion at the request layer — upload size is capped (100 MB by default, configurable via `MUSELAB_MAX_UPLOAD_MB`); `/api/files/grep` has a soft 8-second time budget and a 1 MB per-file cap; `/api/log/client-error` is rate-limited to 30 requests per IP per minute. Other endpoints do not have per-IP rate limiting. If muselab is exposed to more than one trusted user, place a reverse proxy (Caddy or nginx) in front with global rate limits.

## Reporting a vulnerability

Email **hesorchen@gmail.com** with the subject line `muselab security`. Please
do not open a public issue for anything that could be used to read other users'
files or steal tokens. Expect a response within 7 days.
