# Offline evidence synchronization (Phase 10 × Phase 11)

Offline evidence extends the existing offline architecture; there is **no second sync protocol**. Answers still travel through
`POST /api/v1/offline/sync/`; files travel through the ordinary evidence upload endpoint, driven by the same sync engine and ordering.

## Device side (`static/js/offline/evidence-store.js`)

* IndexedDB store `evidence` (schema **v2**, additive migration - no existing record is touched), namespaced by account/organization like
  every other store. An entry holds the **Blob**, name, size, type, SHA-256, caption and state. Passwords/tokens are never stored.
* "Saved on this device" is shown only after the IndexedDB `put` committed. A quota/storage failure throws and stores nothing.
* The entry's `id` (UUID) **is the upload's `operation_id`**: created once, reused on every retry.
* Local validation (type allow-list, size, count, image-only questions, signer name) gives immediate feedback; the server re-validates everything.

### States (what the user is told)

| State | Meaning | Blob |
|---|---|---|
| `local` | saved on this device, waiting to upload | kept |
| `uploading` | request in flight (a reload returns it to `local`) | kept |
| `received` | the server has the bytes; its security check is pending | released (the server owns the file) |
| `available` | **verified by the server** - the only state that counts as evidence | released |
| `failed` | transient retries exhausted; **Sync now** restarts | kept |
| `rejected` | the server refused it (type/content/size/permission/state): needs the user; "Save a copy" and Remove | kept |
| `quarantined` | the server's security check flagged it | released |

"Uploaded", "Submitted" or "Verified" is never shown before the server said so. The Blob is released only after the server acknowledged
durable receipt (a 200/201 with the attachment), and unsynchronized/rejected files block silent deletion (`unsyncedCount`, cache removal needs an
explicit discard, recovery export lists them).

## Upload request (`POST /api/v1/evidence/uploads/`, multipart)

Fields: `assignment_id` (or `execution_id` once known), `item_id`, `operation_id`, `client_sha256`, optional `caption` / `captured_at`, `file`.
No organization, user, status or target type is accepted from the device (rejected as unknown fields).

* **Target resolution is server-side**: the first accepted upload for an assignment starts/resumes its execution through the Phase 9 service;
  an `item_id` from another version, another assignment, another organization or an out-of-scope location is `404`.
* **Completeness**: the server hashes the bytes it received and compares with `client_sha256`; a mismatch is `400 checksum_mismatch` and the
  device retries (nothing acknowledged).
* **Idempotency**: `UNIQUE(uploaded_by, client_operation_id)`; a repeat with the same bytes returns the original attachment (`200`,
  `duplicate: true`) - lost responses and restarts create no second file or attachment; a different file with the same id is `409
  operation_id_reused`. Concurrent duplicates are resolved by the unique index (the loser replays the winner).
* **Authorization is current**: permission, scope, execution state and limits are checked at upload time; a revoked user gets 403/404 and the file
  stays on the device (`rejected`/`local`, never silently dropped).
* Client timestamps are diagnostic (`captured_at`); `uploaded_at` is the server's clock.

## Ordering and submission

1. Each sync run uploads queued files **first**, then asks the server about files still `received` (`GET /evidence/{id}/status/`), then sends
   the answer operations.
2. The device holds a `submit` while any file for that assignment is `local`/`uploading`/`received`/`failed`.
3. The server re-checks at submission: only **available** files count. A file waiting for the check, quarantined or detached yields a
   `validation_failed` rejection that names the reason; the checklist is never submitted on unverified evidence.
4. Answers to evidence questions are never synced as values (`use_evidence_upload`).

## Retry, conflict and revocation

Network errors/5xx/429/checksum mismatches back off (2 s · 2ⁿ⁻¹, capped 5 min, 6 attempts, then `failed`); 401 stops the run without consuming an
attempt; 400/403/404/409/413 are final for that file (`rejected`) with the server's safe message. If the execution was submitted or cancelled
meanwhile the upload is `409` and the local file is kept (export/"Save a copy").

## Limits and limitations

HEIC and Office files are not accepted; photos are re-encoded by the server (the stored hash differs from the device's); there is no resumable/
chunked upload (≤ 10 MB per file, one request); browsers may evict IndexedDB under storage pressure - synchronize promptly; Blobs are not
encrypted at rest by the application ([offline-security-and-privacy.md](offline-security-and-privacy.md)).
