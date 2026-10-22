# 0055. Saving and submission: revisions, locks and one transaction

- **Status:** Accepted
- **Date:** 2026-10-22

## Context
Autosave sends many small writes from one page, possibly from two devices, racing a submission.

## Decision
* Every save locks the execution row (`SELECT ... FOR UPDATE`); submission and cancellation lock the assignment first, then the
  execution (fixed order, no cycles). Saves, submission and cancellation of one execution are therefore serialized.
* Each response carries a `revision`; clients send the revision they last saw. A mismatch is a **conflict for that item** (409 /
  an inline notice), never an overwrite. The execution also has a `revision` bumped by every accepted write.
* A batch is partial: valid, current answers are stored, invalid or stale ones reported. An unchanged value is a no-op.
* The workspace autosaves with HTMX `change` events, debounced and queued (`hx-sync="this:queue last"`), so a newer request always
  carries the newer revisions; the save indicator is rendered by the server after the write.
* Submission re-derives everything from stored responses, is all-or-nothing, and is idempotent (`already_submitted`, no second
  event). The audit entry for a refused submission is written outside the rolled-back transaction.

## Consequences
* Two devices editing one answer see a conflict instead of silently losing data; offline sync (Phase 10) reuses the same tokens.
* Row locks are short-lived and never held across user input or network calls.
