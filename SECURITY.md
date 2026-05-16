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
- Point `MUSELAB_ROOT` at a dedicated sub-directory, never `$HOME` (the app refuses to start if you do)
- Put it behind nginx/caddy with HTTPS; never expose `:8765` to the public internet directly
- Treat `MUSELAB_TOKEN` like a password: long, random, rotated on suspicion of leak
- Add nginx basic auth as a second factor in front of muselab if exposed beyond your LAN

## What we defend against

- Path traversal outside `MUSELAB_ROOT`
- Reading or overwriting credential-shaped files (`.env*`, SSH private keys, `*.pem`, `credentials.json`, etc.) — blocked even with valid token
- Same-origin XSS via uploaded `.html` / `.svg` / markdown — `/api/files/raw` serves arbitrary types as `application/octet-stream` attachments, HTML/SVG are served with a strict CSP + sandbox, and rendered markdown is run through DOMPurify before insertion
- Token length < 16 chars or `MUSELAB_ROOT` pointing at system paths — refused at startup

## What we do NOT defend against

- A compromised `MUSELAB_TOKEN` — full access by design
- Symlink escapes from inside `MUSELAB_ROOT` (don't put attacker-controlled symlinks in your archive)
- Resource exhaustion (no rate limiting on grep / upload size)

## Reporting a vulnerability

Email **hesorchen@gmail.com** with subject `muselab security`. Please do not
open a public issue for anything that could be used to read other users'
files or steal tokens. I'll respond within 7 days.
