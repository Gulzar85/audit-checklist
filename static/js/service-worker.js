/* Checklist Platform - service worker foundation.
 *
 * Phase 0 provides:
 *   - a precached offline app shell,
 *   - network-first caching for static assets (with an offline fallback),
 *   - a network-first strategy for navigations with an offline fallback.
 *
 * The Django admin and the REST API are deliberately *never* handled here.
 * The worker is registered with scope "/", so without this exclusion it would
 * intercept /admin/ navigations and /static/vendor/... asset requests and serve
 * stale copies from the cache - which left the admin with a stale stylesheet
 * and invisible Font Awesome icons. Authenticated admin/API traffic must always
 * come straight from the network.
 *
 * Later phases extend this worker with the offline checklist engine:
 *   - IndexedDB persistence of executions/responses,
 *   - a background sync queue (Background Sync API with a periodic flush),
 *   - retry with backoff and conflict resolution against the server.
 */
"use strict";

const CACHE_VERSION = "cp-static-v2";
const OFFLINE_URL = "/offline/";
const PRECACHE_URLS = [
  OFFLINE_URL,
  "/static/css/app.css",
  "/static/js/app.js",
  "/static/vendor/htmx.min.js",
  "/static/vendor/alpine.min.js",
  "/static/vendor/lucide.min.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

// Routes that must never be intercepted or cached by the offline app shell.
// Admin and API responses are authenticated and server-rendered; the admin's
// own CSS/JS/fonts live under these static prefixes.
const BYPASS_PREFIXES = [
  "/admin/",
  "/api/",
  "/static/admin/",
  "/static/jazzmin/",
  "/static/vendor/adminlte/",
  "/static/vendor/fontawesome-free/",
  "/static/vendor/bootswatch/",
  "/static/vendor/bootstrap/",
];

function isBypassed(pathname) {
  return BYPASS_PREFIXES.some(function (prefix) {
    return pathname.startsWith(prefix);
  });
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_VERSION)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((key) => key !== CACHE_VERSION).map((key) => caches.delete(key)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") {
    return;
  }

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) {
    return;
  }

  // Admin / API traffic is always handled by the network.
  if (isBypassed(url.pathname)) {
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(networkFirstNavigation(request));
    return;
  }

  if (url.pathname.startsWith("/static/")) {
    event.respondWith(networkFirstAsset(request));
  }
});

async function networkFirstNavigation(request) {
  const cache = await caches.open(CACHE_VERSION);
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      cache.put(request, response.clone()).catch(() => {});
    }
    return response;
  } catch (err) {
    const cached = await cache.match(request);
    return cached || (await caches.match(OFFLINE_URL)) || Response.error();
  }
}

// Static assets are fetched network-first so a deploy can never get stuck behind
// a stale, cached copy. The cache is used only as an offline fallback.
async function networkFirstAsset(request) {
  const cache = await caches.open(CACHE_VERSION);
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      cache.put(request, response.clone()).catch(() => {});
    }
    return response;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) {
      return cached;
    }
    throw err;
  }
}

// Placeholder for future offline execution sync.
self.addEventListener("sync", (event) => {
  if (event.tag === "checklist-sync") {
    event.waitUntil(Promise.resolve());
  }
});
