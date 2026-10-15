# Microsoft Entra ID setup

## Library choice
**MSAL for Python** (`ConfidentialClientApplication`), the Microsoft-supported library for the
authorization-code flow with PKCE. One library, lazily imported in `apps/accounts/entra.py`; no
overlapping OIDC package (ADR 0029).

## App registration
1. Entra admin centre -> App registrations -> New registration (single tenant unless you need more).
2. Redirect URI (Web): `https://<host>/auth/microsoft/callback/` (and `http://localhost:8000/...` for dev).
3. Certificates & secrets -> new client secret (store in the secret manager / environment only).
4. API permissions: Microsoft Graph `openid`, `profile`, `email` (delegated). No admin consent needed.
5. Optional: front-channel logout URL `https://<host>/auth/logout/` if you enable federated logout.

## Configuration (environment only)
| Variable | Purpose |
| --- | --- |
| `MICROSOFT_TENANT_ID` | Directory (tenant) id; also the default allowed tenant |
| `MICROSOFT_CLIENT_ID` | Application (client) id |
| `MICROSOFT_CLIENT_SECRET` | Client secret - never committed, stored, or logged |
| `MICROSOFT_REDIRECT_URI` | Must equal the registered redirect URI |
| `MICROSOFT_POST_LOGOUT_REDIRECT_URI` | Where Microsoft sends the browser after logout |
| `MICROSOFT_SCOPES` | Default `openid,profile,email` |
| `MICROSOFT_ALLOWED_TENANTS` | Comma list; empty -> only `MICROSOFT_TENANT_ID` |
| `MICROSOFT_FEDERATED_LOGOUT` | Also sign out of Microsoft on logout |
| `AUTH_PROVISIONING_MODE` | `pending` (default, approval required) or `active` |
| `AUTH_EMAIL_LINKING` | `non_staff` (default), `all`, `disabled` |
| `ENABLE_PASSWORD_LOGIN` | Local fallback; **must be false in production** |
| `AUTH_THROTTLE_MAX_ATTEMPTS`, `AUTH_THROTTLE_WINDOW_SECONDS`, `TRUSTED_PROXY_COUNT` | Throttling |
| `SESSION_ABSOLUTE_TIMEOUT_SECONDS`, `SESSION_IDLE_TIMEOUT_SECONDS` | Session lifetime |

Sign-in is offered only when tenant, client id and secret are all set.

## Provisioning and linking policy
* **First login of an unknown identity**: user created `pending`; an administrator approves
  (admin action "Approve selected users"). No privileges are granted by authenticating.
* **Existing local account, same email**: linked once, only if `AUTH_EMAIL_LINKING` allows it and the
  account is not staff/superuser (default). A different object id on the account is a conflict. Every
  link is logged (`identity.linked`). Email changes in Entra update the existing user (no duplicate).
* **Unlink**: administrator action / `unlink_external_identity` (audited). A user left with no way to
  sign in becomes `inactive`.

## Claims used
object id (`oid`), tenant (`tid`), email / preferred username, display / given / family name, job title.
Only these are persisted; everything else is dropped after authentication.

## Security notes
State, nonce and PKCE are handled by MSAL and kept only in the server session; the client secret is
read from the environment; tenants are validated explicitly; errors never expose configuration;
`next` is validated; the callback is not CSRF-exempt-by-accident (it carries no session-changing POST).