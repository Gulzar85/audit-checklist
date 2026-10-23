# 0066. Corrective actions: relational model, append-only history, derived overdue, completion is not verification

- **Status:** Accepted
- **Date:** 2026-10-23

## Decision
* **One `CorrectiveAction` row per unit of work**, with a real `PROTECT` foreign key to its finding; several per finding, each with its own owner, due date, status, log, evidence and decisions.
  Organization and location are copied from the finding once and kept equal by a database trigger.
* **The work log and the verification decisions are separate append-only tables** (`CorrectiveActionUpdate`, `CorrectiveActionVerification`), enforced by triggers: a correction is a new entry that points at
  the old one; a decision is immutable and unique per submission. A verification can only be inserted for a *submitted* action with that submission number. `django-simple-history` still records the action itself.
* **Lifecycle** `draft -> open -> in_progress <-> blocked -> submitted -> verified -> closed`, with `rejected` (back to the owner), `cancelled` (final) and `reopen` (verified/closed -> in progress, with a reason) -
  a transition table (`constants.TRANSITIONS`) used by one service. **Submission by the owner, verification by someone else, and closure are three events**; none of them touches the finding.
* **Overdue is derived**, never stored: a past `due_at` while the owner still has work (open, in progress, blocked, rejected). Submitted work is not "overdue" for the owner. A queryset filter and a property give the same answer; no job is needed.
* **Policy snapshots on the action** (`independent_verification`, `required_evidence`) keep running work stable when settings change.
* Permissions extend the registry (14 location-scoped codes); ownership is part of the policy: the owner or a manager (`update`) may act, an owner cannot reassign or re-date their own work.

## Consequences
* Reports can compute workload and overdue from the same selectors; Phase 14 subscribes to after-commit signals and calls the services.
* No multi-stage approval, reminders or bulk creation in Phase 13 (extension points documented).
