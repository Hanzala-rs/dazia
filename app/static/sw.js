// DAZIA service worker: caches the app shell only, so the UI can open while offline.
// API responses (/chat, /auth/*, /login, /logout, /health, /reminders, /notifications)
// are never cached: anything not in SHELL is left alone and goes straight to the network.
// Bump CACHE when the shell files change.

const CACHE = "dazia-shell-v2";
const SHELL = ["/", "/static/app.js", "/static/style.css", "/static/icon.svg", "/manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  if (!SHELL.includes(url.pathname)) return; // not part of the shell: untouched

  // Network first; fall back to the cached copy when the laptop can't be reached.
  event.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((cache) => cache.put(url.pathname, copy));
        }
        return res;
      })
      .catch(() => caches.match(url.pathname))
  );
});
