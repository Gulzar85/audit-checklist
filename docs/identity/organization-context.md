# Organization context

A user may belong to several organizations, but each request works within exactly one. This is
"where I am", and it is a security boundary: an id the user cannot see must never become the
scope of a read or a write.

## Storage and resolution

* The choice is stored in the session under `ACTIVE_ORGANIZATION_SESSION_KEY`.
* It is resolved **lazily** by the identity context processor (and `context.get_active_organization`),
  **not** in middleware - so API/JSON responses pay no organization query.
* The stored id is treated as **untrusted**: `selectors.resolve_active_organization` filters it
  through `organizations_visible_to(user)` and falls back to the user's default membership. A
  stale, foreign, malformed or empty id resolves to a safe organization or `None` - never to
  another tenant's.
* The resolved object is memoised on the request (`request.organization`), so the navbar and
  template context share one lookup. The session is written back only when the value actually
  changed, avoiding a session `UPDATE` on every request.

## Templates

The context processor exposes (for authenticated users):

| Variable | Meaning |
| -------- | ------- |
| `current_organization` | The validated active organization, or `None` |
| `password_login_enabled` | Whether the local password form is available |
| `microsoft_login_enabled` | Whether Entra sign-in is configured |
| `entra_authority` | The configured authority (display only) |

Richer collections - a user's memberships, the list of organizations - are provided by the views
that display them (`ProfileView`, `AccountPreferencesView`, `OrganizationSelectView`), keeping
the per-request query count minimal.

## Switching

| Endpoint | Method | Purpose |
| -------- | ------ | ------- |
| `/organizations/select/` | GET | List the organizations the user may switch to |
| `/organizations/switch/` | POST | Set the active organization |

`OrganizationSwitchView` fetches the target through `organizations_visible_to` (so an invisible
organization returns `404` and reveals nothing), then checks `can_switch_organization`, then
`services.set_active_organization`. The `next` parameter is re-validated as a safe internal URL.

The navbar shows the current organization name as the entry point to `/organizations/select/`,
so switching is always reachable and never requires typing a URL.

## Cost

One bounded, indexed query per **authenticated HTML** page (the validated lookup); anonymous and
API pages are unaffected. The two query-count tests in `tests/test_web_locations.py` were raised
by exactly that constant, with the reason recorded in-line.
