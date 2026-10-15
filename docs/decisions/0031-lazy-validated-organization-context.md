# 0031. Organization context: lazily resolved, always validated, never trusted from the client

- **Status:** Accepted
- **Date:** 2026-10-15
- **Relates to:** [0015](0015-tenant-isolation-through-selectors.md) (selectors), [0032](0032-identity-provisioning-and-linking.md) (identity)

## Context

A user belongs to several organizations, but a request works within exactly one. The active
organization is "where I am" - it must survive navigation, but the client can influence the
session, and an organization id the user does not belong to must never become the scope of a
read or a write.

The naive designs each cost something: resolving it in middleware charges every response (API
included) and trusts the session id; trusting a `?organization=` parameter is a tenant-isolation
bug; not having it at all leaves templates and selectors guessing.

## Decision

- The active organization lives in the session (`ACTIVE_ORGANIZATION_SESSION_KEY`) and is
  resolved **lazily in the identity context processor**, not in middleware - so a JSON/DRF
  response pays no organization query at all.
- The stored id is **untrusted input**. `resolve_active_organization()` filters it through
  `organizations_visible_to(user)` (the tenant-isolation choke point) and falls back to a default
  membership; a stale, foreign, garbage or empty id resolves to a safe organization or `None`,
  never to someone else's tenant. It is one indexed query on the fast path.
- The resolved object is memoised on the request (`request.organization` /
  `request._organization_loaded`), so per-request consumers (navbar, template context) do not
  re-query.
- Switching is `POST`-only, goes through the selector (an invisible organization `404`s, so a
  forged id reveals nothing), then a policy check, then `services.set_active_organization`.
  The `next` target is re-validated as a safe internal URL.
- Authorization never reads the session directly for scope: selectors take the user (and, where
  needed, the resolved organization) as explicit arguments.

## Consequences

- One extra, bounded query per *authenticated HTML* page; API and anonymous pages unchanged.
  Query-count tests were raised by exactly that constant, with the reason recorded.
- The "where am I" state cannot leak across tenants: trust is re-established on every request
  from the visible-organizations selector, not from the cookie/session value.
- A first authenticated render may cost one extra fallback query (no session value yet); later
  requests take the validated fast path and skip writing the session when nothing changed.
