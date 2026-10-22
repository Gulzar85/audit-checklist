# Evidence: authorization, organization isolation, retention

## Who can do what

There is no separate evidence permission matrix: **evidence authorization is derived from its target**, so it can never widen access.

| Operation | Rule |
|---|---|
| upload / attach / detach | the target's `check_can_attach`: for a checklist response, `execution.update` at the assignment's location (and the designation rule) **and** an `in_progress` execution |
| view status, preview, download, list | the target's `can_view`: for a checklist response, a *current* `execution.view` or `execution.update` at the location (Phase 9's "the creator keeps access" does not apply to files) |
| attach an existing file | must be able to see the asset through one of its targets, plus the destination's attach rule |
| Django admin | read-only; non-superusers see only evidence of executions they may see; the storage key is not shown; nothing can mark a file clean |

## Isolation and enumeration

* Invisible, foreign-organization, out-of-scope, unknown and malformed ids are all **404** with the same body - a guessed UUID reveals
  nothing. Visible-but-forbidden is **403**; wrong state is **409**.
* Organization consistency is enforced by the service, the target's resolution and a **database trigger** (an attachment, its asset and its
  execution must share one organization). The asset's facts (hash, size, key, type, uploader, organization) are immutable.
* Permission and membership are re-checked on **every** read: a user whose role is revoked loses preview/download immediately.
* Files are served by endpoints, never by path; downloads add `nosniff`, `private, no-store`, a sandboxing CSP and attachment disposition
  (inline only for images). `Referer`/URL guessing cannot reach a file.

## Mutations

POST/DELETE with CSRF (web) or session/token auth (API); upload throttled; unknown request fields (`organization`, `uploaded_by`, `status`,
`storage_key`, `sha256`, `content_type` ...) are rejected, never ignored. Every upload, rejection, attachment, detachment, scan outcome,
quarantine, preview and download is audited with the actor.

## Retention, detachment and deletion

* Evidence is **never hard-deleted**: the database refuses `DELETE` of assets and attachments (trigger). *Detaching* sets
  `detached_at/by/reason`; the file and the history remain and the asset can be re-attached elsewhere.
* A detached file no longer counts as the answer's evidence. A submitted execution's evidence cannot be detached or added (`409`); a later
  correction would need an explicit, authorized business operation that does not exist in this phase (documented limitation).
* Cancelled or archived targets keep their evidence (read-only history). Organization suspension/membership revocation stops access
  immediately (the checks above) without touching the data.
* Physical erasure (legal requests) is an operational procedure on the storage and database by an administrator with database privileges; the
  application deliberately provides no delete path.

## Residual risks

Re-encoding removes metadata and appended payloads from images but cannot make an arbitrary PDF safe: the PDF check is structural plus an
active-content refusal, not a sanitiser - hence PDFs are only ever downloaded and the malware scanner is required in production. Files on
disk are not encrypted by the application (use disk/volume or object-storage encryption).
