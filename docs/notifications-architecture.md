# Notifications architecture (Phase 17)

The notification engine turns **business events** into **in-app notifications** and **email**, with rules, recipients that are resolved and authorized on the server, safe templates, user preferences, an auditable delivery state machine and bounded retries. There is no Redis, Celery, broker, SPA or scheduler: delivery runs after commit (inline by default) and from management commands that a Phase 18 scheduler can call.

```
business module --signal(on_commit)--> integration.py --publish--> NotificationEvent (outbox)
        NotificationEvent --process_event--> rules + recipients + preferences --> Notification (one per event+recipient)
        Notification --> NotificationDelivery (one per channel) --dispatch--> provider --> DeliveryAttempt (append-only)
```

## Four different facts
| Fact | Where | Meaning |
|---|---|---|
| Event created | `NotificationEvent` | a validated business event was recorded |
| Notification created | `Notification` | one person is told; this row *is* the in-app notification |
| Provider accepted | `NotificationDelivery.status = sent`, `accepted_at` | the email provider accepted the message (HTTP 202 / SMTP ok) - **not** that it arrived |
| Delivered | `status = delivered`, `delivered_at` | only when a provider confirms it (`services.confirm_delivery`); no current provider does, so none is ever claimed |

## Publishing events
* Domain modules emit signals **after commit** (`events.send_on_commit`): corrective actions, findings, workflows (task created, instance finished), evaluation (run recorded), assignments, executions. `apps/notifications/integration.py` receives them and calls `services.publish(event_type, source, actor=, discriminator=, metadata=)`.
* `publish` validates the event type and the source type against `registry.EVENTS`/`SOURCES`, keeps only minimal metadata (short scalar values; never answers, evidence or free text) and is idempotent on `(organization, dedupe_key)`.
* **Trade-off (ADR 0079):** the outbox row is written in its own transaction right after the business commit. A process crash between the two loses that event. Callers that must not lose one may call `publish` inside their business transaction; the domain-signal path is accepted for Phase 17 and is cheap to tighten. A rolled-back business change publishes nothing.
* A failure in a receiver is logged and never breaks the business operation.

## Rules
`NotificationRule`: event type, recipient strategies, channels, optional template code, priority, preference policy, filters, `exclude_actor`, precedence. Defaults (`default-<event>`, `is_system_default`) are created lazily per organization, may be edited or disabled and never deleted.
* Several rules may match; per recipient their **channels combine** and the rule with the **lowest `precedence` number** decides template, priority and policy.
* Filters use the Phase 6 operators over facts registered per event (`filters.py`); all must be TRUE.
* Strategies: `assignee`, `creator`, `task_assignees`, `user`, `role`, `permission`. Everything resolves through RBAC at the object's location (`workflows.assignment.candidates`), is limited to active members, then filtered by the **owning module's `can_view`** - receiving a notification never leaks an object. The actor is excluded when the rule says so; the pool is bounded (`MAX_RECIPIENTS`).

## Safe templates
Only `{{ variable }}` over the event's declared variables is rendered (no tags, filters, attribute access, loops). A Django `Engine` with no builtins renders; HTML fields escape values, text fields do not (`Context(autoescape=...)`). Unknown variables and unsafe syntax are refused when the template is saved or previewed. Previews use synthetic values.

## Preferences and policy
Per user, organization and category, per channel; no row means enabled. `optional`: both channels can be switched off; `operational`: in-app always, email can be switched off; `mandatory`: never switched off, and setting it needs `notification.mandatory`. A suppressed channel is recorded (status `suppressed` + reason), not silently dropped.

## Delivery state machine
`pending -> processing -> sent | retry_pending | failed`, `retry_pending -> processing`, `sent -> delivered`, `pending|retry_pending|failed -> cancelled`, `failed -> pending` (manual retry). Database triggers enforce the table and append-only history.
* Claiming uses `select_for_update(skip_locked=True)` with a lock token; a `processing` row older than 600 s is reclaimed and the stale worker's token no longer matches, so it cannot overwrite the result.
* Transient failures (timeouts, connection, 429, 5xx) retry with exponential backoff (60 s x 2^n, capped at 3600 s) up to `NOTIFICATION_MAX_ATTEMPTS` (5). Permanent failures (rejected, unauthorized, not configured, invalid recipient) fail at once. Exhaustion publishes `system.delivery_failed` to holders of `notification.retry`.
* Manual **retry** (failed -> pending, fresh allowance), **resend** (a new delivery with the next sequence key and the reason) and **cancel** need `notification.retry` and a reason, and are audited.
* Idempotency keys: events `(organization, dedupe_key)`; notifications `sha256(event|rule|recipient)`; deliveries `<notification>|<channel>|<seq>`.

## Security
A notification link never authorizes access: opening re-checks the viewer's own permission and the destination is computed, not stored. Notifications are scoped to the recipient in the query (another user's is a 404). Delivery bodies and real addresses never appear in list pages, the admin or the API (only a masked address). Secrets only come from the environment. Audit verbs: `notification.*` (no content, no full addresses, no secrets).

## Retention
`purge_notifications` removes finished history older than `NOTIFICATION_RETENTION_DAYS` (180) in a bounded transaction that raises the database setting `notifications.purge`; the audit log is kept.

See also: [email and Microsoft Graph](notifications-email-and-operations.md), [API](api/notifications.md), ADRs 0079-0081.
