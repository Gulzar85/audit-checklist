# 0046. The answer-type engine: a code registry with schema-validated configuration

- **Status:** Accepted
- **Date:** 2026-10-16
- **Extends:** [0045](0045-generic-checklist-definition-model.md)

## Decision
* Answer-type behaviour lives in **code** (`apps/checklists/answer_types`): one small definition class per type with
  metadata, capabilities, a configuration schema, `normalize` and `serialize`. A central registry is the only place
  that knows which types exist. There is **no table per type**; the database stores `answer_type` and `config` only.
* Configuration is **schema-controlled JSON**, not columns and not free JSON: each setting declares kind, default and
  limits; unknown keys are rejected; the cleaner is idempotent so stored configs re-validate unchanged and older rows are
  read with defaults.
* **One validation path.** Web forms, API serializers and importers hand raw data to `services`, which call the
  registry. Answer validation is DB-free (`AnswerContext`) so the PWA, execution and offline re-validation share it.
* Values are normalized predictably (`Decimal` strings for numerics, UTC for datetimes, option *values* for choices,
  stable matrix keys). Options resolve only against the item's own options; media types are *contracts* (file
  references validated, content verified by the evidence phase), never an upload implementation.
* **Answer ≠ score ≠ finding.** The engine never aggregates, grades or creates findings.
* `ChecklistItem.metadata` (closed key set: `points`, `critical`, `applies_to`, `reference`) holds descriptive facts the
  engine ignores, so source checklists (the two audit workbooks) are loaded without loss and later phases can use them.
  It is deliberately not a free-form field.
* The builder renders type-specific settings from the schema over HTMX instead of one form with every field; a
  design-time preview is server-rendered.

## Consequences
Adding a type is one class (+ a preview branch); the builder form, API metadata and docs follow. Phase 4 configs remain
valid (defaults fill new keys); the normalized config now includes defaults (e.g. a score's `step` and `display`).
A numeric answer serializes as a decimal string; clients must not parse it as a float where precision matters.
