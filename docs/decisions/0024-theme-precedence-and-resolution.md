# 0024. Theme precedence: layered tokens, a separate colour mode, one resolver

- **Status:** Accepted
- **Date:** 2026-10-15

## Context

A request may be affected by built-in defaults, an application theme, an organization theme, a
user preference and a temporary preview. The rules must be obvious and implemented once.

## Decision

Tokens are **layers that merge** (a later layer overrides only what it sets):

1. System defaults (built in, always complete)
2. Application theme (the active system theme)
3. Organization theme (the active theme of the user's organization)
4. Temporary preview (session-only; never saved; policy-checked on every request)

The **colour mode** is resolved separately from the layers:
explicit user choice (light/dark/system) -> the theme's `default_appearance` -> system.
So "user chose Dark" beats an organization whose theme prefers Light; "user chose System" follows
the OS; no preference inherits the theme default. Signed-in users' choice is stored server-side;
anonymous visitors use a cookie holding an enum value; signed-in users ignore the cookie.

Anonymous visitors have no organization context: they get the system layer only and no
organization query is made. A user in several organizations gets the theme of the organization
marked *default*, else the earliest active membership, so there is one consistent look
(inactive organizations are skipped).

`resolve_theme(request)` (`apps/themes/resolution.py`) is the only implementation; templates
(via a context processor), the stylesheet endpoint, the PWA manifest and the API call it. It is
memoised per request and costs at most three small queries (membership, active themes,
appearance); anonymous requests cost one.

## Consequences

- Stored tokens are *overrides*: an organization theme that sets few tokens keeps inheriting the
  rest from the system theme. The editor therefore saves only values that differ from the base.
- Caching is content-addressed (key = hash of the contributing theme ids and `updated_at`), so
  activation, publishing and rollback produce new keys; there is nothing to invalidate and no
  Redis. Any Django cache backend works.
