# Mobile (PWA)

> [简体中文](mobile_zh.md)

muselab ships a Web App Manifest and apple-touch-icon. Once deployed to
your own server, it can be added to the phone home screen and launched
like a native application.

- **Single codebase** serves iOS / Android / desktop — no `.ipa` / `.apk`
  builds, no App Store review.
- **Standalone mode**: no browser address bar or tab bar — full-screen
  app shell.
- **Theme-color aware**: the iOS status bar follows the light / dark
  preference.
- **Touch-optimized**: inputs are at least 16 px (prevents iOS auto-zoom),
  pull-to-refresh is disabled, and the on-screen keyboard pushes the chat
  view up to follow.

## Install on iPhone

> **Requires HTTPS (a secure context).** iOS only registers the Service
> Worker — and only grants Web Push — when the page is served over HTTPS.
> A plain `http://` LAN / IP address (e.g. `http://192.168.x.x:PORT`) is
> **not** a secure context on iOS: the SW never registers, "Add to Home
> Screen" yields a degraded shell, and push permission cannot be granted.
> Two ways to get a secure context:
> 1. **Tailscale** — reach the box via its `*.ts.net` MagicDNS name, which
>    serves HTTPS automatically (no certs to manage). See [quickstart](quickstart.md).
> 2. **Reverse proxy with a real cert** — run
>    [`scripts/setup-https.sh`](../scripts/setup-https.sh) to put Caddy +
>    Let's Encrypt in front of muselab on your own domain.

Open the page in **Safari** (iOS Chrome doesn't expose this menu) →
**Share** sheet → **Add to Home Screen** → Add.

On Android Chrome, the address bar shows an "Install" prompt directly.

> The self-hosting aspect is preserved here: the phone communicates
> directly with the user's own server, with no Apple / Google signed
> binary and no third-party distribution channel in the chain. The
> self-hosting model is not compromised by the install path.

## Web Push notifications

Enabled in **Settings → Notifications**. The backend exposes
`/api/push/{vapid-public,subscribe,unsubscribe}` and provides VAPID keys
via `.env`. Per-device subscriptions persist in the browser. Long scheduled
tasks send a push notification upon completion, even if the tab is closed.

### iOS limitations

- **Add to Home Screen first.** iOS only grants Web Push to a PWA that has
  been added to the home screen and launched in standalone mode — push
  cannot be enabled from a regular Safari tab. Install the app (see above),
  open it from the home screen, then enable notifications.
- **No vibration.** muselab requests `navigator.vibrate()` for notification
  feedback; iOS Safari ignores it. Notifications still appear, just without
  haptics. Android honors vibration.

## Pull-to-refresh

The browser's native pull-to-refresh is disabled app-wide (an accidental
overscroll should not reload a streaming session). The **file tree** has its
own custom pull-to-refresh gesture instead: pull down at the top of the file
list to re-fetch the directory. This is muselab's own implementation, not the
browser's, so it only re-syncs the tree — it never reloads the page.
