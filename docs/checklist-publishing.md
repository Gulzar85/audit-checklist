# Checklist publishing and publication validation

## One validator

`apps/checklists/publication.py::validate_for_publication(version)` returns a `PublicationReport`:

* `errors` - **block** publication;
* `warnings` - informational (never blocking);
* `stats` - sections / items / options / rules;
* `draft_hash` - the manifest hash of the draft's current definition (the concurrency token for `publish`).

The builder, the API, the publish service and imports all use it - there is no second copy of any rule. Configuration is
validated by the **Phase 5** answer-type engine and rules by the **Phase 6** rule validator; the validator only adds the
version-level checks. It is a pure read: it neither writes nor logs. The explicit *Validate* action (`lifecycle.run_validation`)
adds an audit entry, because "a validation was run" is a meaningful event.

## What blocks publication (errors)

| Area | Code | Rule |
|---|---|---|
| Template | `organization_inactive`, `template_archived`, `template_name_missing` | active organization, not archived, named |
| Version | `not_draft`, `bad_version_number`, `invalid_base`, `no_sections` | is a draft of this template, positive number, valid base, at least one section |
| Sections | `section_name_missing`, `invalid_section_code`, `duplicate_section_code`, `bad_section_order`, `empty_section` | named, valid unique codes, contiguous order, at least one active question |
| Items | `question_missing`, `unknown_answer_type`, `invalid_config`, `invalid_metadata`, `invalid_item_code`, `duplicate_item_code`, `bad_item_order` | text, registered type, configuration accepted by the answer-type engine, valid reference data, **version-wide unique** codes, contiguous order |
| Options | `too_few_options`, `fixed_options_mismatch`, `unexpected_options`, `invalid_option_value`, `option_label_missing`, `bad_option_order` | choice types have >= 2 active options; Yes/No types carry exactly the platform options; other types none; valid values |
| Rules | the Phase 6 codes (`unknown_item`, `invalid_operator`, `invalid_value`, `invalid_action`, `cycle`, `conflict`, ...) | every reference resolves inside **this version**, operators suit answer types, values parse, no dependency cycles, no definite conflicts |

Cross-version references cannot occur by construction (children belong to one version; rules reference by code within
their own version) and would be reported as `unknown_item` if they ever appeared.

## Warnings

`no_instructions` (the checklist has no general instructions), `all_optional`, `inactive_items`, `no_change_summary` (a
newer version without a summary), `no_changes` (identical to the published version) and Phase 6's `possible_conflict`,
`inactive_source`, `inactive_target`. Warnings are stored with the version at publication (`publication_report`), so
"what did we publish knowingly?" is answerable later.

Example report (`GET/POST .../validate/`):

```json
{"valid": false, "version": 2, "draft_hash": "sha256:v1:9c1f...", "stats": {"sections": 3, "items": 9, "options": 14, "rules": 2},
 "errors": [{"level": "error", "code": "empty_section", "message": "Section “Food Safety” has no active items.", "subject": "section:S-002"},
            {"level": "error", "code": "unknown_item", "message": "Rule R-001: Unknown question “GONE” in this checklist version.", "subject": "rule:R-001"}],
 "warnings": [{"level": "warning", "code": "no_instructions", "message": "No checklist instructions have been provided.", "subject": "template"}]}
```

## The publish service

`lifecycle.publish_version(version, actor=, change_summary=, expected_hash=)`:

1. open a transaction; **lock the template row** (serialises with edits, other publications and draft creation);
2. lock and re-read the version; refuse anything but a draft of this template (`already_published` -> conflict);
3. validate the **locked** state; blocking errors raise `PublicationBlocked` (nothing is written);
4. if `expected_hash` was given and differs from the draft's hash -> `stale_draft` conflict;
5. mark the previous published version `superseded` (`superseded_at`);
6. publish: `status`, `published_at`, `published_by`, `integrity_hash`, `publication_report`, change summary;
7. history rows (simple_history), `_touch` the template, ActivityLog events (`checklist_version.published`,
   `checklist_version.superseded`); commit.

Nothing is reported as published before the transaction commits. A refused publication writes only the audit entry
(`checklist_version.validation_failed`, plus `checklist_rule.validation_failed` for rule problems) *outside* the rolled-back
transaction. No lock is held while waiting for the user: the dialog only carries the hash.

## Immutability after publication

See [checklist-versioning.md](checklist-versioning.md#immutability-honestly) and ADR 0049. In short: service refusals,
model guards and PostgreSQL triggers; to correct a published definition, create a new draft and publish it.

## Audit trail

| Event (`ActivityLog.verb`) | When |
|---|---|
| `checklist_version.created` | a draft is created |
| `checklist_version.cloned` | ... from a prior version (metadata: source version and copied counts) |
| `checklist_version.validated` / `.validation_failed` | the Validate action; a refused publication |
| `checklist_version.published` | publication (metadata: hash, counts, warning count) |
| `checklist_version.superseded` | the previous version was superseded |
| `checklist_version.discarded` | a draft was deleted |
| `checklist_version.compared` | a comparison was requested |
| `checklist_version.sealed` | maintenance: a legacy version's integrity hash was recorded |
| `checklist.archived` / `checklist.restored` | template archival |
| `checklist_section/item/option/rule.*` | edits of the draft (Phase 4-6) |

`simple_history` records every row change, including bulk-created clones. Events carry `metadata.template_id` for the
template's activity tab; they never contain secrets or copies of answer content.
