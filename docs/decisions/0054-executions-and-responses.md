# 0054. Executions and responses: one live execution per assignment, relational answers, frozen once submitted

- **Status:** Accepted
- **Date:** 2026-10-22

## Context
Phase 9 turns an issued assignment into an attempt with answers. The answers must stay tied to the exact published version, one
attempt must not silently become two, and a submitted checklist must not be editable by any path that skips the services.

## Decision
* `ChecklistExecution` references the assignment (PROTECT) and copies `organization` and `version` from it once; a PostgreSQL
  trigger keeps both equal to the assignment's and immutable. No questions are copied: the version is the definition.
* `ChecklistResponse` is a relational row per (execution, item) holding the **serialized normalized value** from the Phase 5
  validator in a JSON column - not a JSON blob for the whole checklist and not a table per answer type. Its item must belong to the
  execution's version (trigger). A response row is a *current* answer; history is `simple-history` plus the audit log.
* Statuses: `in_progress`, `submitted`, `cancelled`. A partial unique index allows one `in_progress` or `submitted` execution per
  assignment; a cancelled one frees the slot. A new execution after submission is never created implicitly.
* Submitted and cancelled executions are final: the trigger refuses any change to their responses and status. No reopen exists.
* The assignment moves only through two system-only transitions (`assigned -> in_progress`, `in_progress -> completed`, plus
  `in_progress -> assigned` when the only execution is cancelled), kept outside the user-facing transition table.
* Assignments never import executions: cancelling an assignment calls a registered hook that cancels its in-progress execution in
  the same transaction, keeping the answers.

## Consequences
* Re-execution (recurrence) needs a deliberate later capability; duplicates cannot happen by accident.
* A superuser can still bypass triggers; that is an operational control.
* `revision` columns exist solely for optimistic concurrency of autosave (ADR 0055).
