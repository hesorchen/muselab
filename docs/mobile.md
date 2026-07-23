# Mobile (PWA)

> [简体中文](mobile_zh.md)

muselab includes a Web App Manifest and home-screen icons, so it can be
installed as a PWA on iOS and Android. It uses the same code and backend as the
desktop UI, with no `.ipa`, `.apk`, or app-store distribution.

## Installation

PWA features, Service Workers, and Web Push require HTTPS. A plain
`http://192.168.x.x:PORT` URL is not a secure context on iOS.

Use either:

1. a Tailscale `*.ts.net` HTTPS address; or
2. a real domain and certificate, with `scripts/setup-https.sh` configuring the
   reverse proxy.

On iPhone, open the site in Safari and choose Share → Add to Home Screen.
Android Chrome normally exposes an Install action directly.

## Mobile layout

Bottom navigation switches among Files, Preview, and Chat. Each page has its
own scroll container. Native pull-to-refresh is disabled so an accidental
gesture cannot interrupt a session; the file tree keeps its own pull-to-sync
gesture at the top.

The chat header contains a terminal shortcut. It switches to Preview and opens
the terminal manager, whose secondary sheet has a New terminal action and a
Plain shell/Profile selector.

When the soft keyboard opens, the composer and active conversation stay
visible. Touch inputs use at least 16 px text to avoid iOS auto-zoom, and bottom
controls account for the device safe area.

## Mobile chat performance

Mobile mounts only the active conversation pane and does not preload other
sessions. It normally shows the latest 15 bubbles, caps the DOM at 60, caches
120 messages, and loads older history 80 at a time.

Streaming Markdown refreshes less often as text grows. Above 32 KiB, a single
in-flight segment temporarily uses plain text, then receives its final
Markdown, math, and path enhancement at completion. This limits heat, dropped
frames, and soft-keyboard stalls during long replies.

The queue, elapsed turn time, scroll position, draft, and footer state are kept
per conversation tab. Switching pages or resuming the PWA does not reset a
running turn.

## Mobile terminal

The terminal uses a real PTY in Preview. Vertical dragging scrolls terminal
history without moving the whole page. Mobile retains up to 3,000 scrollback
lines to limit memory use.

A horizontally scrollable accessory row provides:

- Copy, Paste, and backslash `\`;
- Ctrl+C, Esc, and Tab;
- Up, Down, Left, and Right.

This covers keys that some mobile keyboards hide. After a transport drop, the
terminal requests a new one-time ticket and reconnects automatically. Switching
pages does not stop the process. See [Terminal](terminal.md).

## Web Push

Enable notifications under Settings → Notifications. On first use, the server
generates the VAPID keypair at
`<primary-workspace>/.muselab/vapid.json`. Registered device subscriptions are
stored at `<primary-workspace>/.muselab/push_subs.json`, not in `.env` or only
in browser-local state.

The browser still owns its PushSubscription and registers it with the server.
Task completion, queue pauses, and other background events can notify the device
while the page is closed. Opening a notification deep-links to the relevant
workspace and conversation. System notifications are suppressed when a visible
muselab window already exists.

### iOS limitations

- Add the app to the Home Screen first, then launch it there and enable push.
- A regular Safari tab cannot enable PWA Web Push for the site.
- iOS Safari ignores web vibration requests, but notifications still appear.

Do not casually delete `vapid.json`. Changing the key invalidates every current
subscription and requires each device to subscribe again.

## Version recovery

When a backgrounded PWA resumes, it checks the frontend/backend asset version.
If the version changed and no stream is active, it reloads automatically to
avoid stale JavaScript talking to a newer server. If the UI remains stale,
fully close and reopen the PWA.
