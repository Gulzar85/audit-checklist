# Service-worker caching policy

`static/js/service-worker.js`, scope `/`, version `CACHE_VERSION = "cp-static-v8"`. A strict **allowlist**; there is no "cache every GET".

| Request | Strategy | Why |
|---|---|---|
| Install | `cache.addAll(PRECACHE_URLS)`: `/offline/`, `/offline/app/`, `app.css`, `app.js`, `components.js`, vendored htmx/alpine/lucide, icons, every `static/js/offline/*.js` module | the offline shell must open with no network; a test fails if a module is missing from the list |
| Navigations | **network only**; on failure `/offline/app/` → cached shell, anything else → cached `/offline/` page | authenticated, server-rendered HTML is never written to a cache; the two fallback pages are public and data-free, so one user's session can never be shown to the next |
| `/static/…`, `/themes/tokens.css` | network-first, cache as offline fallback | a deploy is never stuck behind a stale copy; theme changes show at once |
| `/auth/`, `/organizations/switch/`, `/admin/`, `/api/`, admin static | **not intercepted** | authentication, session probes, administration and all data/sync traffic always go to the network |
| non-GET, cross-origin | not intercepted | the worker cannot alter writes or third-party traffic |

Only responses that are `ok`, same-origin (`basic`) and **without `Cache-Control: no-store` or `private`** are stored.

## Versioning and cleanup

Bump `CACHE_VERSION` whenever the asset set or this policy changes. `activate` deletes every cache whose name starts with `cp-static-`
and is not the current one, then claims clients. Offline modules are cached together, so an offline page never mixes versions of the
engine; the IndexedDB schema version lives in `schema.js` and upgrades never drop unsynchronized data ([indexeddb-schema.md](indexeddb-schema.md)).

## Background Sync

`sync` event `checklist-sync` only posts `{type: "sync-requested"}` to open pages, which run their own single-flight engine. The worker
never touches IndexedDB or checklist data. Where unsupported, nothing is lost: sync happens at the next explicit trigger.

## Testing

`tests/test_offline_pwa.py` asserts: the precache list covers every offline module and every precached asset exists; sensitive prefixes are
bypassed; the only cache write in `navigation` is behind the public-page guard; `no-store`/`private` are respected; no catch-all caching;
old caches are removed; Background Sync only wakes a page. The shell page is checked to carry no user data or CSRF token.
