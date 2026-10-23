# 0072. Tasks, assignment, approvals, separation of duties, deadlines and concurrency

- **Status:** Accepted
- **Date:** 2026-10-23

## Decision
* **Assignee policies** (named users / registered permission / role) resolve against live RBAC at state entry and again on claim / act. A user policy makes one task per eligible person; a group policy makes **one claimable task** - no arbitrary pick, no assumed reporting line. No eligible assignee: *block* (the transition fails) or *attention* (flag + unassigned task + administrator reassigns). Orphaned tasks (inactive assignee) are released to the pool or flagged - never silently dropped.
* **A task is not an authorization grant**: acting needs the task *and* visibility of the record *and* separation of duties; the domain service enforces the actor's own domain permission.
* **Approvals**: `one`, `all` (named users), `min` (N distinct approvers). One approval per actor and state visit (database unique); a rejection ends the step at once; a group task stays open for further approvers.
* **Separation of duties**: controlled options on a state (`not_starter`, `not_target_owner`, `not_target_creator`, `not_previous_actor`) that can only add restrictions to the domain's own rules.
* **Deadlines**: a state's `due_hours` sets the task's `due_at`; overdue is derived on read - no stored flag, no job needed for correctness.
* **Concurrency / idempotency**: the instance row is locked (without joins - PostgreSQL re-checks join predicates against old partner rows when a locked row changed, which can silently drop the row), requests carry a durable idempotency key (`UNIQUE(instance, key)`) and an optional expected revision; tasks are unique per (instance, state visit, slot). Verified with threaded tests (concurrent decisions, retries with one key, concurrent starts).

## Consequences
No reminders or escalation jobs yet; the data (`due_at`, `needs_attention`, events) is ready for them.
