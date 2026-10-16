# 0052. One open assignment per version and location; bulk assignment is all-or-nothing

- **Status:** Accepted
- **Date:** 2026-10-16

## Context
Without a duplicate rule, double-clicks, retries and overlapping bulk runs would create competing assignments for the same work.
Bulk operations touch many locations at once, so partial failure semantics must be chosen deliberately.

## Decision
* **Duplicate policy:** at most one *open* (`assigned`, `in_progress`) assignment per `(version, location)`. A partial unique
  index enforces it under concurrency; the service checks first so the user gets a clear message naming the existing
  assignment (HTTP 409). Cancelled and completed assignments do not block a new one. Drafts are checked by the service
  (creating a second draft or issuing into an open one is refused) but not by the index, so a draft stays a cheap, non-binding note.
* **Bulk is all-or-nothing.** Every target is validated first; any failure refuses the whole request and reports each target.
  Writes run in a single transaction. Replays are safe because their targets are then duplicates.
* Out-of-scope, unknown, malformed and other-organization location ids give the **same generic "not available" result** in bulk,
  so the endpoint is not an oracle for location existence.
* Targets are capped at 200 per request; "include descendants" expands only to locations the user may assign to and reports how
  many were left out.

## Consequences
* Retrying a failed or interrupted request is always safe; a half-created rollout cannot exist.
* A manager who wants "everything except the three that already have it" must remove them (the review step shows exactly which).
* Best-effort bulk (skip failures) was rejected: it makes outcomes harder to reconcile and hides data problems.
