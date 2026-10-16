# Checklist template engine (Phase 4)

Phase 4 defines **what a checklist asks**. It stores no answers, scores, findings or executions - those are
later phases that *consume* these definitions.

```
ChecklistTemplate  (organization, code, slug, category, status)
  └─ ChecklistVersion  (version_number, status draft|published, change_summary)
       ├─ ChecklistSection  (sort_order, is_required)
       │    └─ ChecklistItem  (code, question, answer_type, config, is_required, is_active, sort_order)
       │         └─ ChecklistOption  (value, label, sort_order, is_active)
       └─ ChecklistRule  (source_item, operator, value, action, target_item | target_section)
```

## The engine is not score-centric
There is **one** item model. What differs between a Boolean check, a 1-5 rating and a temperature reading is
`answer_type` plus a small validated `config` - never a separate table, never a `score` column. Option rows carry
no score either; scoring (Phase 15) will be an independent capability that reads responses.

| Answer type | Config | Options |
|---|---|---|
| `boolean` | - | - |
| `yes_no`, `yes_no_na` | - | fixed (created and kept in sync automatically) |
| `single_choice`, `multiple_choice` | - | author-defined (≥ 2 to publish) |
| `score` | `min`, `max` (0 ≤ min < max ≤ 100; default 1-5) | - |
| `numeric` | optional `min`, `max`, `unit`, `decimals` | - |
| `text`, `long_text` | optional `max_length` | - |
| `date`, `datetime`, `photo`, `file`, `signature` | - | - |
| `matrix` | `rows`, `columns` (reserved shape) | - |

Configuration and answer validation are handled by the Phase 5 answer-type engine: see [answer-types.md](answer-types.md),
[answer-validation.md](answer-validation.md) and [checklist-item-configuration.md](checklist-item-configuration.md).

## Versioning, publishing and lifecycle (Phase 7)
A version moves `draft -> published -> superseded`; exactly one version of a template is published (the current one) and a
published definition is immutable down to PostgreSQL. Cloning, the publication validator, the publish service, comparison,
integrity hashes and archival are documented in [checklist-versioning.md](checklist-versioning.md),
[checklist-lifecycle.md](checklist-lifecycle.md), [checklist-publishing.md](checklist-publishing.md),
[version-comparison.md](version-comparison.md) and [api/checklist-versions.md](api/checklist-versions.md)
(ADRs [0048](decisions/0048-version-lifecycle-and-archival.md), [0049](decisions/0049-published-definitions-are-immutable.md),
[0050](decisions/0050-version-manifest-and-integrity-hash.md)).

## Versions and immutability
* A template always has a version history; `create_checklist_template` starts draft v1.
* Only a **draft** version can change. `publish_checklist_version` freezes it (`published_at`); every structure
  service re-reads the version under a template lock and refuses edits with `version_immutable`. A new draft is
  `create_checklist_version` - a *copy* of the latest version with new identifiers.
* One draft per template (partial unique constraint). Phase 7 hardens the release workflow; the schema already lets an
  execution point at a specific `ChecklistVersion`.
* The admin mirrors this: published structure is read-only there too.

## Lifecycle
`draft → active → archived` (one field, `status`). Activation needs a published version. Archiving never deletes and
is reversible (`restore`). Only a draft that was never published can be deleted.

## Identity
`code` is stable and unique per organization (validated, normalised to upper case, never derived from the name,
immutable after creation). `slug` is for navigation only. Items have a stable `code` too, unique within the version
(generated `Q-001` when omitted); an option's `value` is stable while its label can change.

## Ordering
`sort_order` is explicit and contiguous. Unique constraints on `(version, sort_order)`, `(section, sort_order)` and
`(item, sort_order)` are **deferrable**, so a reorder rewrites the numbers inside one transaction. The builder offers
Move up / Move down buttons (keyboard accessible); the services also accept a full ordered id list.

## Rules (Phase 6)
`ChecklistRule` is data: a condition tree (AND/OR groups of comparisons) and a list of actions, referencing questions and
sections by code within one version. Rules are validated as a set (references, operators, values, cycles, conflicts), frozen
with the published version, and evaluated by one deterministic engine shared by the builder, API and (later) execution.
See [conditional-rules.md](conditional-rules.md), [rule-operators.md](rule-operators.md), [rule-actions.md](rule-actions.md),
[rule-validation.md](rule-validation.md), [api/conditional-rules.md](api/conditional-rules.md) and
[ADR 0047](decisions/0047-conditional-rules-engine.md). A section now has a stable `code` (`S-001`) like a question.

## Future-proofing
* **Repeatable sections:** a section is a plain row with its own id, so a later `repeatable`/`min_repeats` column (or
  an execution-time instance model) needs no restructuring.
* **Nested sections:** deliberately not built; a `parent` FK can be added through a controlled migration.
* **Location use:** templates are not tied to locations; assignment (Phase 8) links a version to locations.

## Authorization
Phase 3 RBAC, organization-level (`checklist.view|create|update|publish|delete|manage`), see
`apps/checklists/policies.py` and `docs/access-control-matrix.md`. Selectors start from
`templates_for_user` (organizations where the caller holds `checklist.view`); out-of-scope templates are 404,
visible-but-forbidden actions 403. The API, the builder and imports call the same policies and services.

## Interfaces
* Web: `/checklists/` (list), `create/`, `<id>/` (detail: structure, versions, activity), `<id>/edit/`,
  `<id>/duplicate/`, `<id>/archive/` (POST archive|restore|activate|delete), `<id>/versions/…`, `<id>/builder/…`,
  `<id>/rules/` (rule list), `rules/new/`, `rules/<rule>/edit/`, `rules/simulate/` (the simulator).
* API: `/api/v1/checklists/` (+ `{id}/`, `archive/`, `restore/`, `activate/`, `duplicate/`, `versions/`, `versions/new/`,
  `sections/`, `items/`, `rules/` ...); structure is read-only over the API except items and rules in the draft.
* Import: `apps/checklists/imports.py::import_items` (all-or-nothing, draft versions only, policy-checked per row).
  Admin export is limited to authorized templates.
* Import: `imports.import_rules` / `export_rules` for rules (same guarantees).
* Seed: `python manage.py seed_demo_checklists --organization <slug>` (idempotent; includes the conditional-rules demo
  *Daily Facility Safety Check*), `seed_audit_checklists` for the two audit workbooks.

## Audit
`simple_history` on every model; ActivityLog verbs `checklist.created|updated|activated|archived|restored|duplicated|deleted`,
`checklist_version.created|published`, `checklist_section.added|updated|removed|reordered`,
`checklist_item.added|updated|removed|reordered`, `checklist_option.*`, `checklist_rule.created|updated|enabled|disabled|condition_changed|actions_changed|removed|reordered|validation_failed`; each carries
`metadata.template_id` for the template's activity tab.

## Deferred to later phases
Answer *storage* (the engines for answer validation (5) and conditional rules (6) exist), the release/approval workflow and version
comparison (7), assignment (8), execution (9), offline (10), evidence (11), findings (12), corrective actions (13),
workflow (14), scoring (15). Not built here: drag-and-drop reordering, template-level import/export UI, category
management screen (categories are created from the template form).
