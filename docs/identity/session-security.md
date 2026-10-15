# Session security

| Setting | Value |
| --- | --- |
| `SESSION_COOKIE_HTTPONLY` | `True` (all environments) |
| `SESSION_COOKIE_SAMESITE` / `CSRF_COOKIE_SAMESITE` | `Lax` |
| `SESSION_COOKIE_SECURE` / `CSRF_COOKIE_SECURE` | `True` in production |
| HSTS / SSL redirect | production (`DJANGO_SECURE_*`) |
| CSRF | on for every state change (logout, switch, preferences are POST + token) |
| CSP | unchanged strict policy; no auth page needs inline script/style |

## Lifetime (ADR 0038)
* **Absolute** `SESSION_ABSOLUTE_TIMEOUT_SECONDS` (default 8 h): a sign-in never outlives it.
* **Idle** `SESSION_IDLE_TIMEOUT_SECONDS` (default 30 min, `0` = off).
* Enforced in `IdentityMiddleware` on every request; the idle stamp is written at most once a minute.
* Expired **page** requests: signed out, a flash message, then the normal login redirect (with a safe
  `next`) - no loop. Expired **API / HTMX / JSON** requests: `401`
  `{"error": {"code": "session_expired"}}` with `Cache-Control: no-store`; HTMX also receives
  `HX-Redirect` to `/auth/login/?expired=1&next=...`.
* A user suspended/deactivated mid-session is signed out on their next request.

## PWA
No offline authentication. The service worker never caches `/auth/*` (login, logout, session probe),
`/organizations/switch/`, the admin or the API, so an expired session is never hidden behind a cached
page. `GET /auth/session/` returns `{"authenticated": bool, "expires_in": seconds|null}` (no-store, no
identity data) and does **not** extend the session, so a client can tell *authenticated but offline*
(probe unreachable, last answer authenticated) from *unauthenticated/expired* (probe answers false or a
401). Future offline checklist execution can rely on this contract without redesign.

## Throttling
Failed sign-ins are counted per client address in Django's cache (no Redis). The address is
`REMOTE_ADDR`, or - only when `TRUSTED_PROXY_COUNT=N` - the entry N hops from the right of
`X-Forwarded-For`; a client-forged leftmost value is never believed. The default LocMem cache is
per-process; point `CACHES` at a shared backend if you run several workers.

## Production guard
`config.settings.production` raises `ImproperlyConfigured` if `ENABLE_PASSWORD_LOGIN` is true.