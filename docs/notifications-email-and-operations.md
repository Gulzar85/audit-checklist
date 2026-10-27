# Notifications: email, Microsoft Graph, operations and the Phase 18 contract

## Providers
`NOTIFICATION_EMAIL_PROVIDER` is a dotted path to a `BaseEmailProvider`:
* `apps.notifications.providers.django_smtp.DjangoEmailProvider` (default) sends through Django's `MAILERS`/`EMAIL_*` configuration. Locally use `EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend` (or locmem in tests).
* `apps.notifications.providers.microsoft365.Microsoft365EmailProvider` sends with the Graph `sendMail` endpoint using **application** credentials (client credentials). It never reports success it did not get: 202 means *accepted*; 401/403 -> `unauthorized` (permanent), 429 -> `rate_limited` (transient), 5xx -> `provider_error` (transient), timeouts and connection errors are transient, other 4xx -> `rejected` (permanent), missing configuration -> `not_configured` (permanent, nothing is sent). Tokens, addresses and bodies are never logged.

### Graph setup
1. In Entra ID add the **`Mail.Send` application permission** to the app registration and grant admin consent.
2. Restrict it to one mailbox with an **Exchange Application Access Policy** (`New-ApplicationAccessPolicy ... -AccessRight RestrictAccess`), otherwise the app can send as anyone.
3. Set in the environment: `MICROSOFT_TENANT_ID`, `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`, `NOTIFICATION_GRAPH_SENDER` (the mailbox), optionally `NOTIFICATION_GRAPH_TIMEOUT`, and `NOTIFICATION_EMAIL_PROVIDER=apps.notifications.providers.microsoft365.Microsoft365EmailProvider`.

## Settings
`NOTIFICATION_DISPATCH_INLINE` (True: dispatch right after commit; False: wait for `dispatch_notifications`), `NOTIFICATION_MAX_ATTEMPTS` (5), `NOTIFICATION_BATCH_LIMIT` (50), `NOTIFICATION_RETENTION_DAYS` (180), `NOTIFICATION_BASE_URL` (absolute links in email), `NOTIFICATION_GRAPH_SENDER`, `NOTIFICATION_GRAPH_TIMEOUT`.

## Management commands (all bounded, idempotent, concurrency-safe)
| Command | Purpose |
|---|---|
| `process_notification_events [--limit N] [--no-dispatch]` | outbox events -> notifications and deliveries |
| `dispatch_notifications [--limit N]` | send due first attempts and retries |
| `emit_due_reminders [--within-hours H]` | publish due-soon / overdue corrective-action events (nothing is sent directly) |
| `seed_notification_defaults [--organization slug]` | create default rules |
| `purge_notifications [--days N]` | retention |
| `notification_status` | backlog and failure summary (exit 1 if something failed) |

## Phase 18 contract (no scheduler exists in Phase 17)
A scheduler only has to call, in order: `emit_due_reminders`, `process_notification_events`, `dispatch_notifications`, and occasionally `purge_notifications`. The same entry points are `services.process_pending`, `services.dispatch_batch`, `services.emit_due_reminders` and `selectors.dispatchable_deliveries`. Running them twice, or concurrently, is safe.

## Local development and troubleshooting
* Use the console email backend; deliveries show as `sent` (accepted) and the message is printed.
* Deliveries stay `pending`: `NOTIFICATION_DISPATCH_INLINE` is off or nothing runs `dispatch_notifications`.
* Events stay `pending`: run `process_notification_events`; check `notification_status`.
* `failed` with `not_configured`: Graph settings are incomplete. `unauthorized`: missing consent or access policy.
* No notification for a person: they cannot view the object, are the actor, are inactive/non-members, or a rule/preference suppressed the channel (see the delivery's `suppressed_reason`).
