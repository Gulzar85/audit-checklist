# Private evidence storage

Evidence is **private by default**: there is no public URL, no signed URL and no media route. Every byte is read back through an authorized
application endpoint that re-checks the caller's permission and the file's state.

## Interface (`apps/evidence/storage.py`)

`EvidenceStorage`: `put(key, bytes)` (atomic, never overwrites), `open(key)`, `delete(key)` (compensation/reconciliation only - never
retention), `exists(key)`, optional `keys()`. Select the backend with `EVIDENCE_STORAGE` (dotted path).

### Filesystem backend (shipped)

* Root: `EVIDENCE_ROOT` (default `<project>/private_evidence`, git-ignored). **It must not be served by any web server** and must not overlap
  `STATIC_ROOT`, `MEDIA_ROOT` or `static/` (a test asserts this for the configured paths).
* Keys are 128 random bits fanned out as `ab/cd/<32 hex>`; the pattern is validated and the resolved path is checked to be inside the root,
  so no filename, `..`, absolute path or symlink can reach outside. Writes are temp-file + `fsync` + atomic rename.
* Permissions: owned by the application user, mode `0700` on the root (POSIX). Encrypt the volume at rest (BitLocker/LUKS/cloud disk
  encryption) - the application does not encrypt files itself.

### Object storage (adapter, not bundled)

Subclass `EvidenceStorage` for S3/Azure Blob and set `EVIDENCE_STORAGE`. Requirements: **private bucket/container**, no public ACLs, server-side
encryption, credentials from environment variables (never in code or settings files), versioning/soft-delete enabled, access restricted to the
application identity. Do not enable signed URLs unless you have reviewed that they cannot bypass the per-request authorization; the default
download path is the authorized endpoint.

## Backup and restore

* Back up the database **and** the evidence root together (the rows reference keys; the bytes are not in PostgreSQL). Take the database dump
  first, then the files; a file without a row is a harmless orphan, a row without a file is a restore gap.
* After a restore, run `python manage.py reconcile_evidence --dry-run`: it lists orphan files and reports assets whose file is missing
  (exit status 1). Missing files must be restored from backup - the command never "repairs" them.
* Retention: nothing is deleted automatically; detaching an attachment keeps the asset and the file. Follow the organization's legal
  retention policy for any manual purge, and never delete a file referenced by an `EvidenceAsset` row (the database refuses deleting the row).

## Serving headers

`Content-Disposition: attachment` (inline only for images on the preview route), `X-Content-Type-Options: nosniff`,
`Cache-Control: private, no-store`, `Content-Security-Policy: sandbox; default-src 'none'`, `Cross-Origin-Resource-Policy: same-origin`.
PDFs are always downloads. Nothing uploaded is ever executed or rendered as HTML/SVG in the application's origin.
