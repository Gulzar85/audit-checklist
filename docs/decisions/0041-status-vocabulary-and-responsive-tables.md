# 0041. One status vocabulary and responsive tables

- **Status:** Accepted
- **Date:** 2026-10-15
- **Extends:** [0022](0022-semantic-design-tokens.md)

## Decision
* A single registry (`apps/core/ui.py`) maps a status (`active`, `draft`, `published`, `overdue`, `verified`,
  `rejected` ...) to a semantic **tone**, an **icon** and a **label**. Templates call `{% status_badge value %}`
  and never choose colours; unknown values fall back to *neutral*. Status is therefore always icon + text + tone.
* Tables keep a scrollable wrapper for dense data, and opt in to a **card layout on phones** with the
  `table-stack` class: the shared table template emits `data-label` on each cell and CSS turns each row into a
  labelled card below 640 px. Row actions use one kebab menu partial; bulk actions have a bar partial.
* Loading uses skeletons and button spinners, empty states explain what/why/next, error states use plain language
  with a retry; all are reusable partials.

## Consequences
Future modules (findings, workflows, corrective actions) add their statuses to the registry once and every
badge, table and timeline picks them up.