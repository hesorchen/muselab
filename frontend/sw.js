// muselab service worker — minimal, just for Web Push delivery.
//
// We deliberately do NOT do network caching here. muselab's static
// assets are already cache-busted via ?v=<mtime> in the HTML; adding a
// stale-while-revalidate layer would mostly just confuse the user
// during development. Push is the one capability that NEEDS a SW
// (browsers won't deliver push events to a regular page), so that's
// what we ship.

self.addEventListener("install", (event) => {
  self.skipWaiting();
});
self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) {}
  const title = data.title || "muselab";
  const body  = data.body  || "";
  const tag   = data.tag   || "muselab";
  const url   = data.url   || "/";
  const opts = {
    body,
    tag,
    // Replace previous notification with the same tag (so the user
    // gets one badge per task instead of a stack of repeats).
    renotify: true,
    // Buzz pattern matches the foreground navigator.vibrate one.
    vibrate: [120, 60, 120],
    icon: "/static/assets/icon-512.png",
    badge: "/static/assets/icon-512.png",
    data: { url },
  };

  // Per-device suppression: if any muselab window on *this device* is
  // currently visible (Page Visibility = "visible" — i.e. the user has
  // the app in foreground), they don't need a notification — they'll
  // see the reply land in-app. Each device's SW only sees its own
  // clients, so this is correctly per-device: desktop foreground
  // swallows desktop's push while phone backgrounded still rings.
  //
  // Backend used to do this check via SSE-subscriber count, which broke
  // multi-device (desktop SSE alive => phone push suppressed too).
  // Moving the decision client-side fixes that.
  event.waitUntil((async () => {
    // `force` payloads (settings-page test push) skip the visibility
    // check entirely — the user pressing "send test push" necessarily
    // has a visible muselab window, and suppressing the test on that
    // very device would make the diagnostic look broken.
    if (!data.force) {
      try {
        const clients = await self.clients.matchAll({
          type: "window",
          includeUncontrolled: true,
        });
        const anyVisible = clients.some(c => c.visibilityState === "visible");
        if (anyVisible) {
          // Foreground client renders the reply itself — but it may not know
          // about it yet (e.g. a scheduler run finishing in the background
          // server-side). Tell every client to refresh unread/history state
          // so the bell badge stays live instead of silently swallowing the
          // event with no in-app trace.
          for (const c of clients) {
            try { c.postMessage({ type: "muselab/push-suppressed", url, tag }); } catch (_) {}
          }
          return;
        }
      } catch (_) {
        // matchAll failure (rare) — fall through and show, so we err on
        // the side of NOT silently dropping notifications.
      }
    }
    return self.registration.showNotification(title, opts);
  })());
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";
  // Parse the session id out of the target url (`/?session=<id>`) so we can
  // deep-link the right conversation. New-window navigation carries it in the
  // URL automatically; the focus-existing-tab path needs it postMessage'd
  // since focus() alone doesn't change the tab's location.
  let sessionId = "";
  try { sessionId = new URL(target, self.registration.scope).searchParams.get("session") || ""; } catch (_) {}
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({
      type: "window", includeUncontrolled: true,
    });
    // Tell any live muselab window to ack the unread badge — the user
    // just acknowledged this push by clicking it, so the bell-icon
    // count should clear without making them open the drawer first.
    for (const c of all) {
      try { c.postMessage({ type: "muselab/notification-clicked" }); } catch {}
    }
    // If muselab is already open in a tab, focus it. focus() can't navigate,
    // so also postMessage the target session id and let the app open it.
    // Match by origin + scope-path PREFIX on the parsed URL — the old
    // `c.url.includes(scope)` substring test, with scope "/" expanding to
    // "https://host/", matched EVERY same-origin window (e.g. a raw-file
    // preview opened top-level) and could focus the wrong page.
    const scopeUrl = new URL(self.registration.scope);
    for (const c of all) {
      let cu = null;
      try { cu = new URL(c.url); } catch (_) { continue; }
      const isApp = cu.origin === scopeUrl.origin
        && cu.pathname.startsWith(scopeUrl.pathname)
        && !cu.pathname.startsWith("/api/");
      if (isApp && "focus" in c) {
        if (sessionId) {
          try { c.postMessage({ type: "muselab/open-session", id: sessionId }); } catch {}
        }
        return c.focus();
      }
    }
    if (self.clients.openWindow) return self.clients.openWindow(target);
  })());
});
