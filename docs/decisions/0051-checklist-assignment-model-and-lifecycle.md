# 0051. Checklist assignments: exact version, explicit lifecycle, derived overdue

- **Status:** Accepted
- **Date:** 2026-10-16

## Context
Phase 8 connects checklist definitions to the places they must be done. An assignment must be unambiguous about *what* (which
version), *where* (which location), and *what state it is in*, and must not allow a path that fakes progress.

## Decision
* An assignment references the **exact `ChecklistVersion`** (not the template) and a generic `Location`; both are `PROTECT`.
  Only the *current published version of an active, non-archived template* is assignable (Phase 7 `assert_assignable`), and a
  version whose integrity hash mismatches is refused. Version and location are frozen once the assignment leaves `draft`.
* Status is a **persisted, explicit state machine** (`constants.TRANSITIONS`): `draft -> assigned | cancelled`,
  `assigned | in_progress -> cancelled`, `cancelled -> draft`. `in_progress` and `completed` exist in the vocabulary but are
  settable only by Phase 9 executions. The one writer is `services/lifecycle.py`; forms, serializers and the admin expose no
  `status`.
* **`overdue` is derived** (`due_at < now` while open), never stored. It cannot be missed by a failed job, it clears the moment the
  due date moves, and it needs no scheduler (out of scope).
* Per-organization readable numbers (`ASG-00001`) are allocated under the organization row lock; the UUID is the identity.
* Cancellation is a soft state with who/when/why; issued assignments are never deleted (trigger). Reopen returns to `draft`
  and issuing again revalidates, so a stale commitment cannot slip through.
* Integrity that CHECK constraints cannot express (same organization for assignment/template/location; immutability) is a
  PostgreSQL trigger installed by `post_migrate`, mirroring ADR 0049.
* Notification delivery is **not** built: services emit on-commit signals that nothing listens to yet.

## Consequences
* A new template version never alters an existing assignment; superseding a version does not silently reassign anything.
* "Why is this overdue?" always has a single answer (the due date and the clock), at the cost of computing it per query.
* No exception path exists for assigning a superseded version; the workflow is cancel and re-assign.
