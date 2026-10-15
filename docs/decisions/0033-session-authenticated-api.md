# 0033. The REST API authenticates the session (no separate API tokens yet)

- **Status:** Accepted
- **Date:** 2026-10-15
- **Relates to:** [0017](0017-api-as-thin-adapter.md) (thin adapter), [0008](0008-api-response-contract.md) (contract), [0029](0029-msal-for-entra-sign-in.md) (sign-in)

## Context

The API and the web UI share one identity (one `User`), so the API needs the *same* principal
as the session, not a second credential system. Introducing bearer/API tokens in Phase 2 would
mean token issuance, storage, rotation, expiry and revocation - a new security surface and new
infrastructure (and Phase 2 forbids new infrastructure such as Redis).

## Decision

- `DEFAULT_AUTHENTICATION_CLASSES` is **`SessionAuthentication` only**. The API authenticates
  the caller exactly like a page does: the same session cookie, the same `User`, enforced by
  `IsActiveUser` (a suspended/inactive account gets no API access either).
- Authorization reuses the **policy layer** (`PolicyPermission` delegates to the same
  `apps/*/policies.py` functions the HTML views call) and the **selectors** for queryset
  scoping (`/api/v1/users/` is `users_visible_to(...)`). The API has no security model of its
  own; it only adapts, consistent with ADR 0017.
- `SessionAuthentication` implies CSRF for unsafe methods (DRF enforces it), which is why API
  writes from a browser carry a CSRF token; the test client authenticates directly.
- No Microsoft access/refresh token is ever exposed to or accepted by the API (ADR 0029).

## Consequences

- One identity, one logout, one lifecycle enforcement point: the same middleware that signs a
  suspended user out of the web UI also stops their next API request on the following render.
  Session expiry, `SameSite=Lax`, `HttpOnly` and CSRF apply unchanged.
- When a future phase needs headless/programmatic access, it will add token authentication
  *alongside* sessions rather than replacing it - recorded here so that change is an explicit
  decision, not a silent default.
- Clients outside a browser (scripts, integrations) cannot use the API yet; that is a deliberate
  scope boundary of this phase.
