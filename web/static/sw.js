// Service worker: present so Chrome treats the page as an installable app
// (needed for the Android share target). It caches nothing: the page only
// works when the player is reachable anyway.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => e.respondWith(fetch(e.request)));
