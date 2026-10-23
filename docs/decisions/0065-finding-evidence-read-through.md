# 0065. Finding evidence is read through the triggering response; direct attachment is a later Phase 11 target

- **Status:** Accepted
- **Date:** 2026-10-23

## Context
A finding should show the evidence that supports it, but Phase 11's attachment model has explicit keys per target type and an upload pipeline built around an execution row lock.

## Decision
* A finding shows the **live attachments of its triggering response** (`execution` + `item`) when `inherit_evidence` is on - a read, never a copy; no physical file is duplicated.
* Access is **re-checked through the finding**: the caller must see the finding, the attachment must be one of that finding's own, and only `available` assets are served or listed as downloadable.
  Quarantined/unscanned files are listed with their state and never served. Phase 11 headers and audit apply (`apps/findings/files.py`).
  Viewing a finding does not widen access to the execution or to any other attachment.
* **Direct uploads to a finding are not implemented.** They require a new Phase 11 target type (an explicit `finding` key on `EvidenceAttachment`, a check/trigger update and a `TargetSpec`
  that authorizes through `finding` policies) - a deliberate migration of the evidence module, deferred to avoid destabilising its upload pipeline in the same phase.

## Consequences
* Required-evidence rules keep using Phase 11's availability semantics; nothing unvalidated can satisfy them.
* Manual findings can link an execution/item (and so show its evidence) but cannot carry their own files yet; this is listed as a known limitation.
