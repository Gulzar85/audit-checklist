# 0061. Upload pipeline, lifecycle and scanning: validate bytes, store privately, never default to clean

- **Status:** Accepted
- **Date:** 2026-10-22

## Decision
* **Inspect the bytes, not the client**: extension allow-list (JPG/PNG/WEBP/PDF), real type from magic numbers, images fully decoded and
  re-encoded (metadata and trailing payloads dropped, pixel guard), PDFs structurally checked and refused with active content. HEIC, Office,
  SVG/HTML are not accepted.
* **Private storage behind an interface**; generated keys; traversal-proof; atomic writes; no URLs - files are read only through authorized
  endpoints with `nosniff`, `no-store`, sandbox CSP; inline only for images.
* **File first, rows second, compensate on failure**: the bytes are stored, then the rows are created in one transaction under the execution
  row lock; on failure the file is deleted; a crash leaves a never-public orphan that `reconcile_evidence` removes.
* **Lifecycle** `pending_scan → available | quarantined`, `available → quarantined`; `quarantined` is final and there is no "mark clean".
* **Scanner interface, safe default**: with a scan required (the default) an unconfigured or failing scanner leaves the file pending, unserved and
  uncounted; only an explicit development setting records `not_required` (never "clean"). ClamAV adapter provided; retries by a bounded command
  run from any scheduler - no Redis/Celery.

## Consequences
* Production needs a scanner and encrypted private storage configured externally; both are documented prerequisites.
* Re-encoding changes the stored bytes (the stored hash is the server's); the device's checksum verifies the *transfer*, not the final file.
