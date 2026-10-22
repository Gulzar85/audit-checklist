# 0057. Offline synchronization: per-operation atomic, ordered, idempotent by durable receipts

- **Status:** Accepted
- **Date:** 2026-10-22

## Context
Devices work without a connection and send their changes later, over networks that drop requests and lose responses. Duplicates, partial
application and silent overwrites must be impossible, and the server must stay the authority.

## Decision
* **Reuse Phase 9.** The sync endpoint only calls `start_or_resume`, `save_answers` and `submit`; there is no second execution model and
  no client-created executions. Revisions (ADR 0055) are the conflict detector.
* **Per-operation atomic, no batch rollback**, processed in `seq` order, one result per operation; `submit` is blocked when an earlier
  operation for the same assignment failed. Mixed batches therefore report exactly what happened.
* **Idempotency by a durable `SyncReceipt`** (`UNIQUE(user, operation_id)`), written in the same transaction as the change; a SHA-256
  fingerprint refuses an id reused with a different payload; only terminal outcomes are stored; receipts are pruned by age
  (`prune_sync_receipts`). In-memory or browser-side flags are never relied on.
* **Conflict, not last-write-wins**: a stale `base_revision` returns the server's state and changes nothing. Resolution goes through a
  dedicated, audited, idempotent endpoint and is revalidated like any write.
* **Client timestamps and sequence numbers have no authority.**
* Limits and a throttle scope bound abuse; unknown fields and operation types are rejected.

## Consequences
* A lost response is recovered by resending the same operation; a rejected submit leaves the execution in progress.
* Receipts add one small table; very late retries after pruning degrade to a revision check (a conflict at worst).
