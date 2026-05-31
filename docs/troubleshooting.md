# Troubleshooting

> [简体中文](troubleshooting_zh.md)

Common failures and their fixes. For OS-specific install issues see
[install-linux.md](install-linux.md) / [install-macos.md](install-macos.md).
A quick environment check: `bash scripts/doctor.sh`.

## Access & auth

**Every request returns 401 / "bad token".**
The web UI authenticates with the `MUSELAB_TOKEN` from `.env`, sent as the
`X-Auth-Token` header. Find it with `grep MUSELAB_TOKEN .env` and paste it on the
login screen. (When scripting the API, send `-H "X-Auth-Token: <token>"`, *not*
an `Authorization: Bearer` header.)

**I lost / want to rotate the token.**
Edit `MUSELAB_TOKEN` in `.env` (≥16 chars) and restart, or change it in the
Settings panel (no restart needed). Then log in again — the browser caches the
old one in `localStorage`.

## Models & providers

**Claude models 401, but I'm logged into Pro/Max.**
The backend needs either `~/.claude/.credentials.json` (from `claude login`) or
`ANTHROPIC_API_KEY`. If the installer reported "claude CLI installed but not
logged in", run `claude login` once.

**A third-party provider (DeepSeek/GLM/…) says "invalid api key" with a key I'm
sure is right.**
Confirm the key is set under the *correct* env var (see
[Configuration → Provider keys](configuration.md#provider-keys)). muselab routes
vendor traffic through an isolated CLI config so your Anthropic OAuth is never
sent to them — so a 401 here is genuinely the vendor key.

**MiniMax 401 with a valid key.** China and Global are separate accounts/keys:
`MINIMAX_API_KEY` for `minimaxi.com`, `MINIMAX_INTL_API_KEY` for `minimax.io`.
Set the one matching your account.

**Every send 401s right after a fresh install.**
A session created before any provider was configured used to lock to an
unreachable Claude fallback. Configure a provider in Settings; new sessions then
pick it up, and the composer is gated until at least one model is available. If
an old session is stuck, start a new one.

## Service & port

**Port 8765 is already in use.**
Usually a previous muselab unit. Find it with
`lsof -iTCP:8765 -sTCP:LISTEN`, stop that service, or change `MUSELAB_PORT` in
`.env`. The installer also offers to stop/disable a conflicting unit for you.

**Service won't start.**
Check the logs:

```bash
# Linux
journalctl --user -u muselab -n 50
# macOS
log show --predicate 'process == "muselab"' --last 5m
```

Most often a missing `.env` value (e.g. `MUSELAB_TOKEN` too short) or a port
collision.

**Service stops when I log out (Linux).**
Enable lingering so the user service keeps running:
`sudo loginctl enable-linger $USER`.

## Scheduled tasks

**A task didn't fire exactly on time.** The scheduler loop ticks every ~60 s, so
a run can be up to a minute late. That's expected.

**A task didn't run while the machine was off.** On startup, missed tasks get a
single catch-up run — but only within a 24-hour window. A run missed by more
than a day is skipped, because its prompt was likely contextually stale by then.

See [Scheduled tasks → Security note](scheduler.md#security-note): scheduled runs
execute unattended with full permissions.

## Mobile / push notifications

**iOS won't register the PWA or enable notifications.** iOS requires a secure
context (HTTPS). Plain `http://192.168.x.x:PORT` will not work. Use a Tailscale
`*.ts.net` URL (HTTPS automatically) or run `scripts/setup-https.sh`. Add the
app to your Home Screen *first*, then enable notifications. Full walkthrough:
[Mobile (PWA)](mobile.md).

**Push stopped working for every device at once.** The VAPID keypair at
`<archive>/.muselab/vapid.json` is unreadable. muselab won't silently
regenerate it (that would invalidate all subscriptions). Restore it from backup,
or delete it deliberately to mint a new keypair — every device then re-subscribes.

## Still stuck?

Run `bash scripts/doctor.sh` and open a
[GitHub issue](https://github.com/hesorchen/muselab/issues) with its output.
