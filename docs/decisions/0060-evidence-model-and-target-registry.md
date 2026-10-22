# 0060. Evidence: asset/attachment model, explicit targets and immutable facts

- **Status:** Accepted
- **Date:** 2026-10-22

## Context
Phase 11 needs evidence that is reusable (checklist responses now; findings, corrective actions, verifications later), private, auditable and
safe against cross-organization access - without a client-controlled way to attach files to arbitrary models.

## Decision
* Two tables: `EvidenceAsset` (the stored file and its server-verified facts) and `EvidenceAttachment` (the link to a target). One asset can be
  attached to several targets without copying bytes. No upload-session table: one request carries one ≤ 10 MB file and idempotency is a unique
  `(uploaded_by, client_operation_id)` on the asset.
* **Targets are explicit foreign keys plus a registry**, not a `GenericForeignKey`. A checklist response is `(execution, item)`: it keeps referential
  integrity and the exact version. A `TargetSpec` per type resolves references through the owning module's selector, authorizes through its
  policy and checks its state; adding a type is a deliberate migration + registration.
* **Visibility is derived from the target**, and for files it additionally requires a *current* permission (the Phase 9 "creator keeps access"
  rule is not enough: a revoked user must lose the file).
* **Evidence answers are derived**: the value of a `photo`/`file`/`signature` question is built from its available attachments and validated by
  the existing Phase 5 normalizers; Phase 6 `REQUIRE_EVIDENCE` reads the same data. Typed media values are refused.
* **Database guards** (trigger): organization consistency between asset, attachment and execution; immutable file facts; evidence is never deleted
  - detaching records who/when/why. A check constraint forbids `available` without a safe scan status.

## Consequences
* A new target type costs a nullable key, a constraint/trigger update and one class; nothing in upload, storage, scanning, API or admin changes.
* Submitted executions stay reproducible: version, answers and files cannot change. Corrections after submission are out of scope.
* Denormalized `organization` on both tables is kept consistent by the trigger.
