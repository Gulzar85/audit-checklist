# 0029. Microsoft Entra ID sign-in with MSAL, not a hand-rolled OAuth/OIDC client

- **Status:** Accepted
- **Date:** 2026-10-15
- **Relates to:** [0006](0006-custom-user-entra-ready.md) (the custom, Entra-ready user model)

## Context

Phase 0 built the custom user model "Entra ready" and Phase 2 has to deliver the actual
Microsoft sign-in. The two obvious paths are to write an OpenID Connect client by hand
(authorization-code flow, state/nonce validation, JWT decoding, JWKS handling) or to use
Microsoft's own library, MSAL for Python.

A hand-rolled client would put token validation, key rotation and issuer/audience checking in
our own code, where a mistake is an authentication bypass. MSAL implements exactly that
protocol and is maintained against Microsoft's changes.

## Decision

- Sign-in uses **MSAL for Python** (`msal`) as a confidential-client authorization-code flow
  (`initiate_auth_code_flow` → `acquire_token_by_auth_code_flow`). MSAL validates the ID token
  (signature via JWKS, `state`, `nonce`, PKCE verifier, issuer, audience) - we never parse or
  verify a JWT ourselves.
- We read **only the ID token claims we need** (`oid`, `tid`, email, name, job title) and
  discard the access and refresh tokens. No token, password or client secret is ever stored,
  logged or sent to the client; the client secret lives only in the environment (ADR 0030's
  config surface, `MICROSOFT_*`).
- `apps/accounts/entra.py` is the single seam: configuration, client construction and
  claim→identity mapping. Views and the authentication backend call it; nothing else imports
  MSAL. This is also the seam tests mock, so the suite never needs a Microsoft account.
- The request flow lives in session state only (`OIDC_FLOW_SESSION_KEY` holds the MSAL flow:
  `state`, `nonce`, PKCE verifier, saved server-side). The callback URL is a `GET`, so the
  OAuth `state` parameter validated by MSAL is the anti-forgery guarantee; this is a deliberate,
  documented exception to CSRF-protected POST-only actions.
- Multi-tenant access is denied by default: `tid` must match `MICROSOFT_TENANT_ID` (or be in an
  explicit `MICROSOFT_ALLOWED_TENANTS` allow-list) before any account is provisioned or linked.

## Consequences

- Token validation and key rotation are delegated to Microsoft's maintained library rather
  than being a security surface we own.
- Development and the test suite work without Microsoft at all: configuration absent → the
  Microsoft button is hidden and the local password fallback is used.
- The service layer (`resolve_entra_user`) still decides whether a *verified* identity may
  sign in - MSAL proves who the caller is, it never grants access on its own.
