# 0045. A generic, versioned checklist definition model

- **Status:** Accepted
- **Date:** 2026-10-16

## Decision
* One `ChecklistItem` model for every kind of question. The answer type is data (`answer_type` + validated `config`
  + `ChecklistOption` rows); there are no per-type question tables and no score columns anywhere in the definition.
  Scoring will be a separate capability that reads responses (Phase 15).
* `ChecklistTemplate` (identity, organization, lifecycle) → `ChecklistVersion` (structure snapshot) → sections →
  items → options; rules hang off the version. Structure belongs to a version so a later edit cannot rewrite history.
* A published version is immutable: every structure service takes the template lock, re-reads the version and refuses
  the write. New work happens in a single draft version cloned from the latest (partial unique constraint).
* Lifecycle is one field (`draft | active | archived`); no parallel boolean flags. Archive, never delete; only an
  unpublished draft may be deleted.
* `sort_order` is explicit; uniqueness constraints are deferrable so reorders are one transaction.
* Rules are generic data (`operator`, `value`, `action`, target) - not boolean columns.
* Definitions are organization-level: they use organization-wide `checklist.*` permissions (ADR 0042), not location
  scope. Reuse across locations happens at assignment time.

## Consequences
Any answer type - including ones not yet imagined - needs one enum value and one config rule, not a schema redesign.
Item codes are unique per section in the database and per version in the service (a version-level DB constraint
would need a denormalised version column; deferred until needed).
