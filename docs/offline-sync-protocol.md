# Offline synchronization protocol

Request (`POST /api/v1/offline/sync/`, session + CSRF, `Cache-Control: no-store`):

```json
{"schema_version": 1, "operations": [
  {"operation_id": "<uuid>", "type": "upsert_response", "seq": 12, "assignment_id": "<uuid>", "version_id": "<uuid>",
   "item_id": "<uuid>", "value": "4.5", "not_applicable": false, "comment": "", "base_revision": 1,
   "client_created_at": "2026-10-22T10:00:00Z", "schema_version": 1},
  {"operation_id": "<uuid>", "type": "submit", "seq": 13, "assignment_id": "<uuid>", "version_id": "<uuid>"}
]}
```

Supported operations: `upsert_response`, `clear_response`, `submit`. Unknown operation types, unknown fields (including `status`,
`organization`, `execution`…), malformed ids, oversized values and a different `schema_version` are rejected **per operation**
(`invalid_operation`). Limits: 200 operations, 1 MB body, 20 KB per value, throttle scope `offline_sync` (120/min).

Executions are never created by the client: the first accepted operation for an assignment starts (or resumes) its execution through the
Phase 9 `start_or_resume`. `execution_id`, if sent, must match.

## Response

```json
{"schema_version": 1, "counts": {"applied": 1, "conflict": 0, "rejected": 1, "blocked": 0},
 "results": [{"operation_id": "…", "status": "applied", "response_revision": 2, "execution": "<uuid>", "duplicate": false}, …]}
```

One result per operation, **in request order**.

| `status` | Meaning | Stored receipt |
|---|---|---|
| `applied` | stored (a repeat reports `duplicate: true`) | yes |
| `conflict` | nothing changed; carries the server's current state (`server`) and a `code` | yes |
| `rejected` | nothing changed; final for this payload (`validation_failed` + `errors`/`problems`, `forbidden`, `unavailable`, `stale_version`, `execution_mismatch`, `operation_id_reused`, `invalid_operation`) | yes (except `unavailable` and malformed) |
| `blocked` | not attempted: an earlier operation for the same assignment in this batch failed (`dependency_failed`) | no |

## Contract: per-operation atomic, ordered

* Operations are processed in **`seq` order** (ties by position), each in its own transaction; there is no batch rollback. Independent
  answers are applied independently; the order exists so `submit` runs after the answers it depends on.
* A `submit` is **blocked** when an earlier operation for the same assignment in the batch was rejected or conflicted. The client also
  holds a submit back while anything else for that assignment is unresolved, and the server re-checks completeness itself.
* Client `seq` and timestamps carry no authority: validation, not the claimed time, decides, and `submitted_at` is the server's clock.

## Idempotency

* `SyncReceipt(user, operation_id)` is unique. The receipt is written in the **same transaction** as the change, so "applied but not
  recorded" cannot happen. A retry (lost response, restart, duplicate Background Sync) finds the receipt and gets the original result with
  `duplicate: true`; nothing is applied twice and no second audit event is written.
* The receipt stores a SHA-256 **fingerprint** of the canonical payload. The same id with a different payload → `operation_id_reused`
  (rejected, nothing applied).
* Two simultaneous requests with the same id: the unique index lets one commit; the other rolls back and replays the winner's receipt.
* Transient failures (5xx, network) store nothing, so a retry is processed normally.
* Receipts are scoped to the user. Retention: `prune_sync_receipts --days 90`; a very late retry after pruning is a *new* operation and is
  still checked against the current revision (worst case: a conflict, never a silent double write).

## Revisions and conflicts

Each response has a server `revision` (Phase 9). `base_revision` is the revision the client last saw (0 for a new answer). A mismatch is a
`conflict` with `code: revision_mismatch` - see [offline-conflict-resolution.md](offline-conflict-resolution.md).

## Client behaviour (`sync-engine.js`)

* One run at a time (promise lock + Web Locks per namespace where available). Triggers: Sync now, `online`, a debounced run after local
  saves, Background Sync, and **one** timer for the earliest retry - no polling.
* Each run first re-asserts identity (`GET /offline/session/`); a signed-out or different account stops the run and keeps the queue.
* Entries are sent `sending`; a reload turns leftover `sending` entries back into `pending` (safe: ids make the retry idempotent).
* Network error / 5xx / 429: backoff `2s · 2^(n-1)` capped at 5 minutes, at most 6 attempts, then `failed` until **Sync now**.
  401/403 do not consume attempts. 4xx refusal of the whole request marks entries `failed` with the server's message.
* Local records change only **after** the server's result arrived and was validated (`applyResult`): `applied` → entry removed, answer
  `synced`, `baseRevision` = acknowledged revision, chained entries unlocked; `conflict` → entry + conflict record, answer `conflict`;
  `rejected` → answer `failed` with the message (a new edit replaces the rejected entry).
