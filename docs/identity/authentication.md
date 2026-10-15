# Authentication

Two ways in, one identity. Microsoft Entra ID is presented first; a local email/password form
is an administrative fallback that is switched off with `ENABLE_PASSWORD_LOGIN=False`.

| Endpoint | Method | Purpose |
| -------- | ------ | ------- |
| `/auth/login/` | GET, POST | Sign-in screen (Microsoft button + local form) |
| `/auth/microsoft/login/` | GET | Start the MSAL authorization-code flow |
| `/auth/microsoft/callback/` | GET | Entra redirect target: validate, resolve, sign in |
| `/auth/error/` | GET | Safe failure page (maps a reason code to a message) |
| `/auth/logout/` | POST | Sign out (optionally federated) |

## Microsoft Entra ID flow

1. **Start.** `MicrosoftLoginView` builds an MSAL confidential-client flow
   (`initiate_auth_code_flow`) with `openid profile email` by default, stores the flow dict -
   `state`, `nonce`, PKCE verifier - in the session under `OIDC_FLOW_SESSION_KEY`, and
   redirects the browser to Entra's authorization URI.
2. **Return.** `MicrosoftCallbackView` pops the flow (single use) and calls
   `acquire_token_by_auth_code_flow`. **MSAL** validates the state, nonce, PKCE verifier and the
   signed ID token - the application never decodes or verifies a JWT itself.
3. **Resolve.** `claims_to_identity()` maps only `oid`, `tid`, email, name and job title into an
   `ExternalIdentity`. `resolve_entra_user()` (services) then finds, links or provisions the
   Django user, and `authenticate(request, msal_identity=…)` runs it through `EntraOIDCBackend`.
4. **Sign in.** On success `login()` establishes the session; on refusal a safe reason code is
   recorded and the browser is sent to `/auth/error/`.

Access and refresh tokens returned by MSAL are **discarded** - they are never stored, logged or
returned to the client, because the platform does not call Microsoft Graph.

### Security properties

* The client secret comes only from the environment (`MICROSOFT_CLIENT_SECRET`); no secret is in
  code, templates or the repository.
* The tenant is validated (`tid` must equal `MICROSOFT_TENANT_ID`, or be in
  `MICROSOFT_ALLOWED_TENANTS`) **before** any provisioning or linking happens.
* The callback is a `GET`, so it is not CSRF-protected; the OAuth `state` verified by MSAL is
  the anti-forgery guarantee. This is a deliberate, documented exception.
* `next` targets are validated with `url_has_allowed_host_and_scheme` (same host only); an
  external target is dropped.

### Failure handling

Failures never surface provider detail. Each maps to a vague message on `/auth/error/`:

| Reason code | Shown to the user |
| ----------- | ----------------- |
| `tenant_not_allowed` | Organization not permitted to sign in |
| `identity_conflict` | An account exists but is not linked to this Microsoft identity |
| `account_pending` | Awaiting administrator approval |
| `account_suspended` / `account_inactive` | Account suspended / inactive |
| `missing_email` | No usable email address |
| `invalid_state` / `invalid_request` / `access_denied` / `invalid_client` | Could not complete sign-in |
| `authentication_failed` | Generic retry message |
| `rate_limited` | Too many attempts; wait and retry |

The `reason` and a structured audit entry (`services.record_authentication`) are logged; the raw
`error_description` is not.

## Local password fallback

`EmailAuthenticationForm` signs in the custom `User` (email as the identifier). The backend is
present only when `ENABLE_PASSWORD_LOGIN=True`. In production, once Entra is configured, set it
to `False`. Users provisioned from Entra have an unusable local password.

## Sign-in throttling

Failed sign-in attempts are rate-limited per client address using the configured cache (no new
infrastructure - `CACHES` defaults to `LocMemCache`; point it at a shared backend for multiple
workers).

* `AUTH_THROTTLE_MAX_ATTEMPTS` (default `10`) failures per `AUTH_THROTTLE_WINDOW_SECONDS`
  (default `300`).
* Only **failures** count; a successful sign-in clears the counter, so a legitimate user is never
  locked out by their own valid credentials. `GET` of the form is never blocked.
* The password form and the Microsoft callback are throttled under separate scopes; a blocked
  attempt records `rate_limited` and returns HTTP `429` with the safe error page.

## Mid-session lifecycle enforcement

`IdentityMiddleware` runs on every request. If an authenticated user's `can_sign_in` is false
(e.g. an administrator suspended them while they were signed in), the request is logged out,
the active organization is cleared, and the next navigation shows the sign-in screen with a
warning. There is no window in which a revoked account keeps working.

## Sign-out

`/auth/logout/` is `POST`-only (CSRF-protected) and clears the session. With
`MICROSOFT_FEDERATED_LOGOUT=True` the browser is also redirected to Microsoft's end-session
endpoint, returning to `MICROSOFT_POST_LOGOUT_REDIRECT_URI` (or the local sign-in page).

## Development without Microsoft

With no `MICROSOFT_*` values configured, sign-in is simply unavailable: the Microsoft button is
hidden, `entra.is_configured()` is false, and the local password fallback is used. Tests mock at
the `entra.build_client()` boundary, so the suite never needs a Microsoft account, and there is
no hidden bypass that could reach production.
