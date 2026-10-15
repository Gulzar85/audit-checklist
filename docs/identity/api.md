# Identity API

Two endpoints, both session-authenticated (ADR 0033) and wrapped in the standard response
envelope (`{"data": …, "meta": …}`). A suspended or inactive account is refused
(`IsActiveUser`).

## `GET /api/v1/me/`

Returns the caller's own identity. Read-only fields include the Entra-owned profile, lifecycle
and staff state, which the API never lets a caller change:

```json
{
  "data": {
    "id": "…",
    "email": "audrea.auditor@example.com",
    "display_name": "Audrey Auditor",
    "first_name": "Audrey",
    "last_name": "Auditor",
    "job_title": "Auditor",
    "phone_number": "",
    "avatar_url": null,
    "status": "active",
    "is_staff": false,
    "is_entra_linked": true,
    "can_sign_in": true,
    "last_login": "2026-10-15T09:31:00Z",
    "date_joined": "2026-09-01T08:00:00Z"
  },
  "meta": {}
}
```

## `PATCH /api/v1/me/`

Updates **application-managed** fields only. `phone_number` is writable; `email`, name, job
title and `status` are read-only (they come from Entra or the lifecycle). A request that tries
to set a read-only field simply leaves it unchanged.

## `GET /api/v1/users/`

Lists the users the caller may see - the queryset is `users_visible_to(user)`, the same
tenant-isolation selector the web UI uses, so a caller can never enumerate another tenant's
people (a platform staff user sees everyone). Paginated with the project default (25 per page).

Supported query parameters:

| Parameter | Effect |
| --------- | ------ |
| `search` | Case-insensitive match on email, display name, first or last name |
| `status` | Filter by lifecycle (`pending`, `active`, `suspended`, `inactive`) |
| `organization` | Only users with an active membership in that organization |

`GET /api/v1/users/{id}/` retrieves a single user *if it is in the caller's visible set*;
anything else `404`s. The summary serializer deliberately omits sensitive identity data
(`entra_object_id`, tenant, contact details).

## Authorization

`PolicyPermission` delegates to the same `apps/accounts/policies.py` functions the HTML views
use - the API has no security model of its own (ADR 0017). Queryset scoping (selectors) is the
first line; the policy hook is the second.

## Schemas

Both endpoints are annotated with `drf_spectacular`; the OpenAPI document is served at
`/api/docs/` and validated in CI (`spectacular --validate --fail-on-warn`).
