# Authentication flow

```
Browser -> /auth/login/ -> "Sign in with Microsoft" -> /auth/microsoft/login/
  -> MSAL builds the authorization-code request (state, nonce, PKCE kept in the server session)
  -> redirect to Microsoft -> user authenticates there
  -> /auth/microsoft/callback/?code&state
  -> MSAL validates state/nonce and exchanges the code (server-side, client secret from env)
  -> ID-token claims -> ExternalIdentity (object id, tenant, email, names)
  -> tenant allowed? -> resolve_entra_user -> lifecycle check -> login() -> session -> redirect (validated `next`)
```

1. **Initiate** (throttled): stores the MSAL flow in the session and redirects. Only `next` values that
   pass `url_has_allowed_host_and_scheme` are remembered.
2. **Callback** (throttled on failures): MSAL verifies state, nonce and the token; the application never
   hand-validates a JWT. Tokens are used in memory and discarded - none is stored, logged or sent to the
   browser.
3. **Resolve**: look up by `entra_object_id`. Known -> sync profile fields. Unknown but email matches an
   existing local account -> controlled link (never a privileged account by default; a different object id
   is a conflict). Otherwise provision a new user, `pending` by default.
4. **Decide**: Entra success never overrides `status`. `pending|suspended|inactive` are denied with a safe
   message; the pending/linked account is still saved for an administrator.
5. **Session**: Django session (HttpOnly, SameSite=Lax, Secure in production). The active organization is
   resolved lazily and validated against live memberships on every request.

## Errors
Shown to the user as one of: `invalid_state`, `authentication_failed`, `access_denied`,
`invalid_request`, `invalid_client`, `invalid_identity`, `missing_email`, `tenant_not_allowed`,
`identity_conflict`, `account_pending`, `account_suspended`, `account_inactive`, `membership_missing`,
`user_not_allowed`, `rate_limited` (messages in `exceptions.REASON_MESSAGES`: vague on purpose).
Technical detail goes to the server log and the activity log (reason code only).

## Logout
`POST /auth/logout/` (CSRF-protected): records the event, flushes the Django session, and - when
`MICROSOFT_FEDERATED_LOGOUT=true` - redirects to Microsoft's end-session endpoint. GET is refused.

## Local development without Microsoft
Automated tests mock the MSAL client at its boundary (`FakeMsalClient`); no Microsoft account is
needed. For manual work, `ENABLE_PASSWORD_LOGIN=true` (the default outside production) enables the
local password fallback. It is **not** a hidden bypass: it is explicit configuration and
`config.settings.production` **refuses to start** if it is enabled.