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
  event.waitUntil(self.registration.showNotification(title, opts));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({
      type: "window", includeUncontrolled: true,
    });
    // If muselab is already open in a tab, focus it. Otherwise spawn one.
    for (const c of all) {
      if (c.url.includes(self.registration.scope) && "focus" in c) {
        return c.focus();
      }
    }
    if (self.clients.openWindow) return self.clients.openWindow(target);
  })());
});
