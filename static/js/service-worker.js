/* Checklist Platform - service worker.
 *
 * Cache policy (docs/service-worker-caching.md) - a strict ALLOWLIST, not "cache every GET":
 *
 *   1. PRECACHE (install): the public, data-free offline shell (/offline/app/), the offline fallback page, the compiled CSS, the
 *      application and offline-engine scripts, the vendored libraries and the icons.  Nothing user-specific.
 *   2. STATIC ASSETS (/static/..., the theme stylesheet): network-first with the cache only as an offline fallback, so a deploy is
 *      never stuck behind a stale copy.  A response is stored only if it is a successful same-origin response WITHOUT
 *      `Cache-Control: no-store`/`private`.
 *   3. NAVIGATIONS: network only.  Authenticated, server-rendered HTML pages are never written to the cache.  If the network fails,
 *      the offline shell is served for /offline/app/ and the generic "You are offline" page for everything else - neither contains any
 *      user data, so a previous user's session can never be shown to the next one.
 *   4. NEVER handled (not intercepted, never cached): /auth/ (sign-in, callbacks, session probe), /organizations/switch/, /admin/,
 *      /api/ (authenticated data and the sync protocol) and the admin's static assets.
 *
 * Data for offline checklists lives in IndexedDB (static/js/offline), never in these caches.  Bump CACHE_VERSION whenever the asset
 * set or this policy changes; activation deletes every older cache of this application.
 *
 * Background Sync is an enhancement: a `checklist-sync` event only wakes an open page, which runs its own single-flight engine.
 */
"use strict";

const CACHE_VERSION = "cp-static-v8";
const CACHE_PREFIX = "cp-static-";
const OFFLINE_URL = "/offline/";
const OFFLINE_APP_URL = "/offline/app/";
const THEME_STYLESHEET = "/themes/tokens.css";
const PRECACHE_URLS = [
  OFFLINE_URL,
  OFFLINE_APP_URL,
  "/static/css/app.css",
  "/static/js/app.js",
  "/static/js/components.js",
  "/static/vendor/htmx.min.js",
  "/static/vendor/alpine.min.js",
  "/static/vendor/lucide.min.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  // The offline engine (ES modules): every file must be listed so the shell opens without a network.
  "/static/js/offline/offline-app.js",
  "/static/js/offline/offline-ui.js",
  "/static/js/offline/schema.js",
  "/static/js/offline/database.js",
  "/static/js/offline/database-migrations.js",
  "/static/js/offline/validators.js",
  "/static/js/offline/rules-engine.js",
  "/static/js/offline/evidence-store.js",
  "/static/js/offline/package-cache.js",
  "/static/js/offline/execution-store.js",
  "/static/js/offline/sync-queue.js",
  "/static/js/offline/sync-engine.js",
  "/static/js/offline/conflict-handler.js",
  "/static/js/offline/connectivity.js",
  "/static/js/offline/account.js",
  "/static/js/offline/memory-store.js",
  "/static/js/offline/service-worker-registration.js",
];

// Routes that must never be intercepted or cached.  Authenticated traffic always comes straight from the network.
const BYPASS_PREFIXES = [
  // Sign-in, sign-out and the session probe: a cached login page or session answer would hide an expired session.
  "/auth/",
  "/organizations/switch/",
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

// Only successful, same-origin, cacheable responses are stored (never `no-store` / `private`).
function isCacheable(response) {
  if (!response || !response.ok || response.type !== "basic") {
    return false;
  }
  const control = (response.headers.get("Cache-Control") || "").toLowerCase();
  return control.indexOf("no-store") === -1 && control.indexOf("private") === -1;
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
        Promise.all(
          keys
            .filter((key) => key.indexOf(CACHE_PREFIX) === 0 && key !== CACHE_VERSION)
            .map((key) => caches.delete(key))
        )
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

  // Admin / API / authentication traffic is always handled by the network.
  if (isBypassed(url.pathname)) {
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(navigation(request, url));
    return;
  }

  // The theme stylesheet is part of the app shell: network-first (a theme change shows at once), with the last copy as the
  // offline fallback.  The URL carries a content hash (?v=...), so a cached shell and its cached theme always match.
  if (url.pathname.startsWith("/static/") || url.pathname === THEME_STYLESHEET) {
    event.respondWith(networkFirstAsset(request));
  }
});

// Pages: network only.  Only the two public, data-free pages are kept for the offline fallback.
async function navigation(request, url) {
  try {
    const response = await fetch(request);
    if ((url.pathname === OFFLINE_APP_URL || url.pathname === OFFLINE_URL) && isCacheable(response)) {
      const cache = await caches.open(CACHE_VERSION);
      cache.put(url.pathname, response.clone()).catch(() => {});
    }
    return response;
  } catch (err) {
    if (url.pathname === OFFLINE_APP_URL) {
      const shell = await caches.match(OFFLINE_APP_URL);
      if (shell) {
        return shell;
      }
    }
    return (await caches.match(OFFLINE_URL)) || Response.error();
  }
}

// Static assets are fetched network-first so a deploy can never get stuck behind a stale, cached copy.
async function networkFirstAsset(request) {
  const cache = await caches.open(CACHE_VERSION);
  try {
    const response = await fetch(request);
    if (isCacheable(response)) {
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

// Background Sync (where supported): wake an open page so it can run its own synchronization.  The worker never reads IndexedDB or
// the network for checklist data itself.
self.addEventListener("sync", (event) => {
  if (event.tag === "checklist-sync") {
    event.waitUntil(
      self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
        clients.forEach((client) => client.postMessage({ type: "sync-requested" }));
      })
    );
  }
});
