# Evidence API

Base `/api/v1/`. Envelope and authentication as the rest of v1. Everything is visibility-scoped by the **target**: unknown / foreign /
invisible → `404`, forbidden → `403`, wrong state → `409`. Storage keys are never returned.

| Method | Path | Purpose |
|---|---|---|
| GET | `/evidence/limits/` | allow-list and limits (`extensions`, `content_types`, `max_file_bytes`, `max_request_bytes`, `max_pixels`, `max_per_target`, `scan_required`) |
| POST | `/evidence/uploads/` | multipart upload + attach in one request (**201**; **200** `duplicate` for an idempotent repeat) |
| GET | `/evidence/{id}/` | asset metadata (`id`, `name`, `kind`, `content_type`, `size`, `sha256`, `status`, `scan_status`, `width`, `height`, `uploaded_at`, `available`) |
| GET | `/evidence/{id}/status/` | `{status, scan_status, available, retry}`: `pending_scan` (check pending), `available` (verified), `quarantined` |
| GET | `/evidence/{id}/download/` | the bytes as an attachment (re-authorized; `404` while pending/quarantined) |
| GET | `/evidence/{id}/preview/` | the bytes inline - **images only** |
| GET | `/attachments/?execution=&item=&include_detached=` | paginated attachments of an execution the caller may see |
| POST | `/attachments/` | attach an existing, available asset: `{asset_id, execution_id, item_id, caption?}` |
| GET | `/attachments/{id}/` | one attachment (detached ones stay readable as history) |
| DELETE | `/attachments/{id}/` | **detach** (204, idempotent): the file and history are kept; `409` once the execution is submitted |
| GET | `/targets/{target_type}/{target_id}/attachments/` | live attachments of a target (`checklist_response`: `target_id` = the execution, `?item=` narrows) |

## Upload

```
POST /api/v1/evidence/uploads/   (multipart/form-data)
  target_type   optional, default "checklist_response" (must be registered)
  execution_id | assignment_id    one of them (an assignment starts/resumes its execution server-side)
  item_id       the question
  file          the file
  caption       optional (signature: the signer's name)
  operation_id  optional UUID - the idempotency key (offline devices send one per file)
  client_sha256 optional SHA-256 of the bytes sent; the server verifies what it received
  captured_at   optional, diagnostic only
```

Unknown fields (`organization`, `uploaded_by`, `status`, `storage_key`, `sha256`, `content_type`...) are **rejected** with `400`.

`201`/`200`:

```json
{"id": "<attachment id>", "target_type": "checklist_response", "execution": "...", "item": "...", "caption": "",
 "attached_at": "...", "detached_at": null, "duplicate": false,
 "asset": {"id": "...", "name": "site.jpg", "kind": "photo", "content_type": "image/jpeg", "size": 18211, "sha256": "...",
           "status": "available", "scan_status": "clean", "width": 1024, "height": 768, "uploaded_at": "...", "available": true}}
```

## Errors (`error.details.code`)

| Status | code | When |
|---|---|---|
| 400 | `type_not_allowed`, `content_mismatch`, `invalid_image`, `image_too_large`, `invalid_pdf`, `pdf_active_content`, `empty_file`, `file_too_large`, `too_many_files`, `checksum_mismatch`, `invalid_request` | validation |
| 403 | - | not permitted on that target |
| 404 | `target_not_found` / `asset_not_found` | unknown, foreign, invisible or malformed reference |
| 409 | `already_submitted`, `not_editable`, `operation_id_reused`, `already_attached`, `asset_not_available` | state or idempotency conflict |
| 429 | - | throttled (`evidence_upload`) |
| 503 | `storage_failed` | the file could not be stored (nothing was created; retry) |

Offline devices use this endpoint unchanged - see [offline-evidence-sync.md](../offline-evidence-sync.md). `GET /offline/manifest/` advertises
`evidence: "supported"` and the limits.
