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

Chrome → ⋮ menu → **Share** → **Add to Home Screen** → Add.

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

## Roadmap

Service Worker for offline UI cache — file a feature request on
[GitHub Issues](https://github.com/hesorchen/muselab/issues) if this would
be useful to you.
