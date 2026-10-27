# Notifications API (`/api/v1/notifications/`)

Responses use the standard envelope (`{"data": ..., "meta": ...}`; lists carry `meta.count`; use `?limit=` (max 100) and `?offset=`). Unknown request fields are rejected. A notification of another user, or a rule/delivery of an organization the caller cannot administer, is a 404. No endpoint creates notifications, deliveries or attempts, or marks a delivery sent or delivered.

| Method and path | Purpose | Needs |
|---|---|---|
| `GET /` | my notifications (`unread`, `category`, `archived` filters) | sign-in |
| `GET /{id}/`, `POST /{id}/read/`, `POST /{id}/archive/` | one of mine | owner |
| `GET /unread-count/` | `{"unread": n}` | sign-in |
| `POST /mark-read/` | `{"ids": [...<=200]}` or `{"all": true, "category": ""}` | sign-in |
| `GET/PUT /preferences/` | `{"preferences": [{"category", "in_app_enabled", "email_enabled"}]}` | sign-in |
| `GET /metadata/` | events, variables, strategies, channels, priorities, policies, categories | sign-in |
| `GET /rules/`, `GET /rules/{id}/` | rules | `notification.view` |
| `POST /rules/`, `PUT /rules/{id}/`, `POST /rules/{id}/enable|disable/` | manage rules (mandatory policy also needs `notification.mandatory`) | `notification.configure` |
| `GET /templates/`, `POST /templates/`, `POST /templates/{id}/activate|archive/` | templates | view / configure |
| `POST /templates/preview/` | validate + render with synthetic values (`valid`, `problems`, `rendered`) | `notification.view` |
| `GET /deliveries/`, `GET /deliveries/{id}/` | delivery history and attempts (masked address, no body) | `notification.deliveries` |
| `POST /deliveries/{id}/retry|resend|cancel/` | `{"reason": "..."}` required, audited | `notification.retry` |

Errors: 400 (validation, with a `code`), 403 (permission), 404 (not visible), 409 (conflicting state).
