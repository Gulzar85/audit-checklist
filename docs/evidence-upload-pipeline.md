# Upload pipeline, supported types and limits

## Supported formats (allow-list, `apps/evidence/validation.py`)

| Type | Extensions | Notes |
|---|---|---|
| Photographs | `jpg`, `jpeg`, `png`, `webp` | fully decoded and **re-encoded**: EXIF/GPS and any appended payload are removed |
| Documents | `pdf` | structure checked (`%PDF-` header, `%%EOF` trailer); PDFs with `/JavaScript`, `/JS`, `/Launch`, `/EmbeddedFile`, `/RichMedia`, `/XFA`, `/OpenAction`, `/AA` are refused; PDFs are **never** rendered inline |
| Signatures | PNG / JPEG image (+ the signer's name as the caption) | same image pipeline; max 1 MB |

Not supported (documented limitation): HEIC/HEIF (cannot be re-encoded safely without extra libraries), Office documents, SVG/HTML (active
content), archives, video. The Phase 5 answer-type universe is wider; the evidence allow-list is the intersection of that configuration and
this list.

## Configurable limits (environment)

| Setting | Default | Meaning |
|---|---|---|
| `EVIDENCE_MAX_FILE_BYTES` | 10 MB | per file (a question may set a lower `max_size_mb`) |
| `EVIDENCE_MAX_REQUEST_BYTES` | 12 MB | whole upload request |
| `EVIDENCE_MAX_PIXELS` | 40 000 000 | decompression-bomb guard |
| `EVIDENCE_MAX_PER_TARGET` | 10 | attachments per question (a question may set a lower `max_files`) |

## The stages

1. **Authenticate** (session or API auth) and apply the `evidence_upload` throttle scope (`API_THROTTLE_EVIDENCE_UPLOAD`, default `60/min`).
2. **Resolve and authorize the target** through the registry (organization, scope, existence, version, state). Unknown/foreign → 404;
   forbidden → 403; submitted/cancelled → 409.
3. **Checksum of the received bytes** (optional `client_sha256`): a mismatch means the transfer was truncated or corrupted → `400
   checksum_mismatch`, nothing is stored or acknowledged.
4. **Inspect** (`inspect_upload`): size, name sanitisation (a *display* name only), extension allow-list and the question's own list, the
   real type from the bytes (a renamed JPEG, an executable, SVG or HTML is `content_mismatch`), type-specific validation, re-encode, SHA-256 of
   the stored bytes.
5. **Idempotency** (`operation_id`): an existing asset for `(user, operation_id)` with the same hash returns the original attachment
   (`200 duplicate`); a different hash → `409 operation_id_reused`.
6. **Store** the bytes in private storage under a generated key.
7. **Create the rows in one transaction** under the execution row lock: re-check the target state (a submit may have won), enforce the
   per-target limit, create the asset and the attachment, bump the execution revision, audit.
8. **Scan** synchronously and boundedly (the scanner has a timeout). The asset becomes `available` only if the scanner said clean (or the
   explicit development policy says no scan is required); an error leaves it `pending_scan`; infected → `quarantined`.
9. **Failure handling:** if the transaction fails the stored file is deleted (compensation). If the process dies in between,
   `reconcile_evidence` removes the orphan (see [operations](evidence-operations.md)). A file is never reachable by URL, so an orphan is never public.

Refusals are audited (`evidence.rejected` with the stable code) and returned as `{"error": {"details": {"code": ..., "file": [...]}}}` with a
safe message - no paths, no stack traces.

## What the server never trusts

Original filenames as paths, client MIME types, client checksums (verified, not believed), client organization/user ids (the upload
accepts neither - unknown fields are rejected), client timestamps (`captured_at` is stored for diagnostics only), client target types
(resolved server-side).
