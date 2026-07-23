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
[Configuration](configuration.md)). muselab routes
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

See [Scheduled tasks](scheduler.md): scheduled runs
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

## Conversation streams, queues, and footer

**Sending says it could not start the stream.**
Confirm the reverse proxy allows `/api/chat/stream/start`. The browser first
uses header authentication to request a one-time ticket, then opens SSE with
that ticket. Do not allow only `/api/chat/stream`. Check the service log and the
`stream/start` status in the browser network panel.

**The reply finished, but the page still says it is running.**
Allow one automatic recovery cycle: the client uses an activity probe and disk
replay to reconcile state. If it remains stuck for more than a minute, reload;
persisted output is not lost. If it repeats, check whether the reverse proxy
buffers SSE or blocks the 15-second heartbeat.

**Queued messages did not continue.**
Errors, manual stop, user questions, and permission requests pause the
server-side queue. Return to that conversation and choose Resume or Clear from
the queue notice. Closing the browser does not cancel the queue.

**The footer timer or context looks wrong after switching tabs.**
Return to the conversation and allow one state sync. Elapsed time is anchored
to the server turn start, and context comes from the SDK. Reloading does not
stop a server-side turn that is still running.

## Terminal

**I cannot find New terminal.**
Open the `>_` terminal manager from the preview header or mobile chat header.
New terminal is in the secondary window header, with the Profile selector
below it.

**I selected a Profile, but the default command ran.**
Confirm the selector shows the desired Profile before creating. On mobile,
close the system picker before tapping New terminal. If it persists, reload to
resync Profiles. Editing a Profile never changes an already-running terminal.

**The terminal does not scroll on mobile.**
Drag vertically with one finger inside the terminal body, not from the
accessory-key row. Terminal scroll is isolated from page scroll. Mobile
scrollback is capped at 3,000 lines.

**My mobile keyboard has no backslash or control keys.**
Use the terminal accessory row for `\`, Ctrl+C, Esc, Tab, and arrows.

**Clicks or swipes type fragments such as `2;276;`.**
These are mouse-coordinate reports left enabled after a full-screen terminal
app exits abnormally, not shell-generated input. The current frontend drops
and resets stale reports in the normal shell buffer and isolates mobile
scrolling. Reload a page that was already open.

**Switching tabs or reloading types `0;276;0c`.**
A device query in historical output was being executed again during replay,
causing xterm to send a fresh response to the current shell. The current
frontend recognizes replay boundaries and does not forward replay-generated
responses. Reload after upgrading.

**The terminal cannot connect or keeps reconnecting.**
Confirm the reverse proxy permits WebSocket upgrades and allows both
`/api/terminals/{id}/ticket` and `/api/terminals/{id}/ws`. Terminal tickets are
short-lived and single-use; they must not be cached or reused.

**Terminal limit reached.**
Close unused running terminals, or change `MUSELAB_TERMINAL_MAX_SESSIONS` and
restart. See [Terminal](terminal.md).

## Still stuck?

Run `bash scripts/doctor.sh` and open a
[GitHub issue](https://github.com/hesorchen/muselab/issues) with its output.
