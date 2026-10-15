# 0038. Session lifetime, expiry responses and PWA behaviour

- **Status:** Accepted
- **Date:** 2026-10-15

## Context
Sessions used Django's two-week default with no idle limit, and an expired session produced HTML
redirects even for API/HTMX callers. The PWA must tell "authenticated but offline" from "unauthenticated".

## Decision
* Absolute limit `SESSION_ABSOLUTE_TIMEOUT_SECONDS` (8 h) and idle limit `SESSION_IDLE_TIMEOUT_SECONDS`
  (30 min) are enforced in `IdentityMiddleware` using two session stamps; the idle stamp is refreshed at
  most once a minute, so sessions are not rewritten per request. `SESSION_COOKIE_AGE` matches the absolute
  limit.
* Expired pages: sign out + flash message + the normal login redirect (safe `next`, no loop). Expired
  API/HTMX/JSON requests: `401 {"error": {"code": "session_expired"}}`, `no-store`, plus `HX-Redirect`.
* `GET /auth/session/` reports `authenticated` and `expires_in` without extending the session.
* The service worker never caches `/auth/*` or the organization switch. No offline authentication.
* Production refuses to start with the local password login enabled; the throttle trusts only
  `TRUSTED_PROXY_COUNT` hops of `X-Forwarded-For`.

## Consequences
Behaviour is configurable per environment (`0` disables the idle limit). A shared cache is needed for the
throttle to be global across workers.