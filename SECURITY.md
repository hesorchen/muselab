# Security Policy

## Threat model

muselab is a single-user, self-hosted web app. It gives whoever holds the
`MUSELAB_TOKEN` the ability to:

- Read, write, upload, and delete files under `MUSELAB_ROOT`
- Drive a Claude Agent SDK session running with `permission_mode="bypassPermissions"` and `cwd=MUSELAB_ROOT`

This is intentional — muselab is an AI archive manager, not a sandbox. The
practical implication is that **a leaked token is equivalent to remote code
execution on `MUSELAB_ROOT`**. Operate accordingly:

- Run the service as a dedicated unprivileged user (not `root`, not your login user)
- Point `MUSELAB_ROOT` at `$HOME` or a sub-directory you own (system paths like `/`, `/etc`, `/root`, `/home`, `/var`, `/usr`, `/boot` are refused at startup)
- Put it behind nginx/caddy with HTTPS; never expose `:8765` to the public internet directly
- Treat `MUSELAB_TOKEN` like a password: long, random, rotated on suspicion of leak
- Add nginx basic auth as a second factor in front of muselab if exposed beyond your LAN

## What we defend against

- Path traversal outside `MUSELAB_ROOT`
- Reading or overwriting credential-shaped files (`.env*`, SSH private keys, `*.pem`, `credentials.json`, etc.) — blocked even with valid token
- Same-origin XSS via uploaded `.html` / `.svg` / markdown — `/api/files/raw` serves arbitrary types as `application/octet-stream` attachments, HTML/SVG are served with a strict CSP + sandbox, and rendered markdown is run through DOMPurify before insertion
- Token length < 16 chars or `MUSELAB_ROOT` pointing at system paths — refused at startup
- Constant-time token compare (`hmac.compare_digest`) — no timing side-channel for guessing the secret
- Default response headers: `X-Content-Type-Options: nosniff` (no MIME sniffing of file previews) · `Referrer-Policy: same-origin` (token in query strings never leaks via cross-origin `Referer`) · `X-Frame-Options: SAMEORIGIN` (external sites can't iframe-phish the UI)
- `noindex, nofollow, noarchive` meta + `/robots.txt` — accidental public exposure won't get crawled

## What we do NOT defend against

- A compromised `MUSELAB_TOKEN` — full access by design
- Symlink escapes from inside `MUSELAB_ROOT` (don't put attacker-controlled symlinks in your archive)
- Resource exhaustion at the request layer — upload size IS capped (100 MB default, override via `MUSELAB_MAX_UPLOAD_MB`), `/api/files/grep` has a soft 8 s time budget + 1 MB per-file cap, and `/api/log/client-error` is rate-limited to 30 requests/IP/minute. Other endpoints don't have per-IP rate limiting. If you expose muselab beyond a single trusted user, put a reverse proxy (caddy / nginx) in front with global rate limits.

## Reporting a vulnerability

Email **hesorchen@gmail.com** with subject `muselab security`. Please do not
open a public issue for anything that could be used to read other users'
files or steal tokens. I'll respond within 7 days.
