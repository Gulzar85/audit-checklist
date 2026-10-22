# 0062. Offline evidence reuses the Phase 10 engine and the evidence upload endpoint

- **Status:** Accepted
- **Date:** 2026-10-22

## Decision
* No new protocol: the device queues the file Blob in a new IndexedDB store (schema v2, additive) and the existing single-flight sync engine uploads
  it with `POST /api/v1/evidence/uploads/` *before* the answer operations, so a submit always follows its files.
* **The entry id is the upload's idempotency key** (`operation_id`); the server's unique `(user, operation_id)` makes retries, lost responses and
  restarts safe. `client_sha256` lets the server refuse a truncated transfer before acknowledging anything.
* **A file counts only when the server says it is available.** Local states are labelled honestly (saved here, waiting, uploading, received/check
  pending, available, failed, rejected, quarantined); the device holds a submit until nothing is unverified, and the server re-checks at submission.
* **Nothing is silently deleted**: the Blob is released only after the server acknowledged receipt; rejected/failed files keep their Blob and can be
  copied out; removing a cached assignment with such files needs an explicit discard.
* The offline evaluator (version 2) supports `REQUIRE_EVIDENCE` and evidence answers derived from local entries; compatibility now accepts evidence
  checklists. The server remains authoritative for permissions, validation, the scan and the final decision.

## Consequences
* Evidence checklists work offline within browser storage limits; Blobs are not encrypted at rest and may be evicted by the browser.
* No resumable/chunked uploads: a failed upload re-sends the file (≤ 10 MB).
