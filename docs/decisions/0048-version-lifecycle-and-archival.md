# 0048. Version lifecycle: draft, published, superseded; archival belongs to the template

- **Status:** Accepted
- **Date:** 2026-10-16
- **Extends:** [0045](0045-generic-checklist-definition-model.md)

## Context
Phase 4 had `draft` and `published` versions and a template-level `archived` status. Phase 7 needs a lifecycle that keeps old
definitions readable and reproducible, says which version operations may start from, and cannot contradict itself.

## Decision
* Version statuses are **`draft -> published -> superseded`** (`ChecklistVersion.status`, the only source of truth). A template
  has at most one draft and **exactly one published version at a time** (partial unique indexes), so "the current version" is
  a query, not a flag that can disagree.
* Publishing a version supersedes the previously published one **in the same transaction**. A superseded version is immutable,
  readable and never deleted. It is **not assignable** for new work (`lifecycle.assert_assignable`); references made while it
  was current stay valid. "Keep using the old version until an assignment ends" is a decision for the assignment phase; the rows
  are preserved to allow it.
* **No version-level `archived` status.** Archiving is the template's (`TemplateStatus.ARCHIVED`): an archived template refuses
  edits, new drafts, publication and assignment, keeps every version untouched, and is restored only by an explicit,
  authorized transition (`checklist.manage`). A second flag on versions would permit contradictory states ("published version
  of an archived template") for no benefit.
* Allowed transitions are a table (`ALLOWED_TRANSITIONS`) enforced by `lifecycle.assert_transition`, the
  model's `save()`, and PostgreSQL. Rolling back is **a new draft copied from a historical version**, never an edit of history.
* A draft can be **discarded** (deleted) because nothing can rely on it; the only version of a checklist cannot be discarded
  (delete the draft checklist), and `lifecycle.protected_dependencies` is the hook later phases use to refuse deletions.
* Version numbers are `max(existing) + 1` computed under the template row lock and backed by a unique constraint; a discarded
  draft's number is reused (it was never published or referenced). `based_on` records lineage (clones and rollbacks).
* Existing permissions are reused (no new codes): view -> history/compare/preview; update -> create/discard draft, validate;
  publish -> publish; manage -> archive/restore. Decisions about *who* are in `policies.py`.
* The data migration marks older published versions of a template as superseded (one published per template).

## Consequences
* "Current version" queries are trivial and unambiguous; assignments (Phase 8) reference a version id and check assignability.
* Superseded versions accumulate by design; history pages are paginated.
* Template metadata (name, description, general instructions, category, duration) stays editable after publication: it is
  descriptive, not part of the version definition (ADR 0050).
* A draft can be left behind: it blocks new drafts until published or discarded. That is deliberate (one working copy).
