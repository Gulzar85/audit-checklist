# 0081. Preferences, retries and retention

- **Status:** Accepted
- **Date:** 2026-10-27

## Decision
* **Preference policy** per rule: `optional` (in-app and email can be switched off), `operational` (in-app always, email can be switched off), `mandatory` (cannot be switched off; needs the separate `notification.mandatory` permission). The platform invents no legal obligation: "mandatory" is the organization's own decision. Suppression is recorded.
* **Retries:** transient failures use bounded exponential backoff (60 s x 2^n, capped at one hour, 5 attempts); permanent failures fail immediately; exhaustion raises an operational notice to holders of `notification.retry`. Claiming uses `FOR UPDATE SKIP LOCKED` with a lock token and a 600 s stale-lock reclaim. Manual retry/resend/cancel need a permission and a reason.
* **Microsoft Graph** uses application credentials (`Mail.Send`, restricted by an Exchange Application Access Policy to one mailbox) and never reports unconfirmed success.
* **Retention:** finished history is deleted by `purge_notifications` (180 days by default) in a bounded transaction that sets `notifications.purge`, the only setting the append-only triggers honour; the audit log is kept.

## Consequences
Users keep control of optional noise; operations have a bounded, observable pipeline; the database protects history from accidental edits or deletes.
