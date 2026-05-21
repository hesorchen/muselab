# Mobile (PWA)

> [简体中文](mobile_zh.md)

muselab ships a Web App Manifest + apple-touch-icon, so once you've
deployed it to your own server you can add it to your phone's home screen
and launch like a native app.

- **One codebase** serves iOS / Android / desktop — no `.ipa` / `.apk`
  builds, no App Store review.
- **Standalone mode**: no browser address bar / tab bar — full-screen
  app shell.
- **Theme-color aware**: iOS status bar follows your light / dark
  preference.
- **Touch-tuned**: inputs ≥ 16 px (defeats iOS auto-zoom), pull-to-refresh
  disabled, on-screen keyboard pushes the chat to follow.

## Install on iPhone

Chrome → ⋮ menu → **Share** → **Add to Home Screen** → Add.

On Android Chrome the address bar shows an "Install" prompt directly.

> The self-host angle matters here: your phone talks straight to *your*
> server, with no Apple / Google signed binary, no third-party
> distribution channel in the chain. The whole point of self-hosting
> isn't violated by the install path.

## Web Push notifications

Enabled in **Settings → Notifications**. Backend exposes
`/api/push/{vapid-public,subscribe,unsubscribe}` and ships VAPID keys via
`.env`; per-device subscription persists in the browser. Long scheduled
tasks fire a push when they finish even if the tab is closed.

## Roadmap

Service Worker for offline UI cache — track in [TODO.md](../TODO.md).
