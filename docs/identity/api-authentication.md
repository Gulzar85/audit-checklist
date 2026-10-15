# API authentication

The API authenticates with the **Django session** (DRF `SessionAuthentication`), so a request resolves to
the same `User` as the browser (ADR 0033). No separate token system and no Microsoft access token is ever
issued to, or accepted from, an API client. Authentication and authorization stay separate: identity is
established first, then selectors (scope) and policies (permission) decide.

## Endpoints
| Endpoint | Purpose |
| --- | --- |
| `GET/PATCH /api/v1/me/` | the caller (only `phone_number`/avatar editable) |
| `GET/PATCH /api/v1/me/preferences/` | `appearance` (theme engine) + `timezone`, `language`; `null` clears; unknown keys 400 |
| `GET /api/v1/users/` | users sharing an *active* membership with the caller (search, `status`, `organization`) |
| `GET /api/v1/users/{id}/` | same scope; anything else is 404 |
| `GET /api/v1/organizations/...` | only organizations with an active membership (404 otherwise) |
| `GET/PUT /api/v1/preferences/appearance/`, `GET /api/v1/themes/active/` | theme engine |

## Rules
* Unauthenticated -> 403 (no `WWW-Authenticate` challenge for session auth); an *expired* session ->
  401 JSON `session_expired`; suspended/inactive user -> signed out and denied.
* The `organization` query parameter is a filter *inside* the caller's visible set, never an escape from it.
* Inactive (`suspended`/`left`) membership removes access immediately.
* Errors use the standard envelope; pagination and filtering follow `docs/architecture/api.md`.

## Future
If non-browser clients are needed, add a token scheme (for example OAuth client credentials validated by
the identity provider) as a *new* DRF authentication class resolving to the same `User`; the
organization/permission layers need no change.