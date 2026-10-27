# 0079. Notification events, the outbox and delivery after commit

- **Status:** Accepted
- **Date:** 2026-10-27

## Decision
* Business modules emit signals with `transaction.on_commit`; `apps/notifications/integration.py` turns each into one validated `NotificationEvent` (the **outbox**), idempotent on `(organization, dedupe_key)`. Notifications are created from the stored event, so a failure can be retried and nothing is lost once the event row exists.
* **Accepted trade-off:** the event row is written after the business commit, so a crash in that window loses that event. Callers that cannot accept this call `publish` inside their own transaction (the outbox row then commits atomically). A rolled-back change publishes nothing.
* Notification, delivery and attempt are separate rows (an in-app notification is the `Notification` itself; each channel has its own state machine). *Event created*, *notification created*, *provider accepted* and *delivered* are different facts; `delivered` needs provider confirmation and is never inferred.
* Dispatch happens inline after commit by default (`NOTIFICATION_DISPATCH_INLINE`) and otherwise through `dispatch_notifications`; without Redis/Celery a scheduler (Phase 18) only has to call the commands.

## Consequences
Simple, transactional and testable; at-least-once creation with idempotency keys; a documented window for the domain-signal path.
