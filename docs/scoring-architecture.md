# Scoring architecture (Phase 15)

The scoring engine turns the answers of a **submitted checklist execution** into points, a percentage, a grade and critical-failure flags - by the rules configured on the **exact published checklist version** the execution used. It does not validate answers (Phase 5), decide what is hidden or required (Phase 6), or move any lifecycle (executions, findings, corrective actions, workflows): it reads their results.

## Components (`apps/scoring`)

| Module | Responsibility |
|---|---|
| `constants.py` | the finite vocabularies: methods, N/A and missing-answer policies, result statuses, critical conditions, scorable types, limits, `ALGORITHM_VERSION` |
| `config.py` | the configuration as data: exact-decimal **parsing**, **validation** against the version's real questions, canonical form and `config_hash`. Pure |
| `engine.py` | **`calculate(config, facts, final=)`** - the single authoritative calculation. Pure: no database, clock or randomness |
| `models.py` | `ScoringConfig` (one per checklist version) and `ScoreResult` (append-only snapshot) |
| `guards.py` | PostgreSQL triggers: draft-only configuration, append-only results, consistency |
| `services/configuration.py` | writing/validating a **draft's** configuration; the hooks the version lifecycle calls (seal, publish validation, clone) |
| `services/runtime.py` | live provisional score, the final snapshot at submission, authorized recalculation, the designer's what-if |
| `policies.py`, `selectors.py` | authorization and scoped reads |
| `integration.py` | registers with `apps.checklists.hooks` and `apps.executions.hooks` - those apps never import scoring |
| `views.py`, `urls.py`, `templates/scoring/` | the editor, item-rule form, what-if, execution score page; `apps/api/v1/scoring.py` the REST API |

Web UI, REST API, offline synchronization and the tests all use the same `engine.calculate`; views, serializers and templates contain no scoring arithmetic.

## Data model

* **`ScoringConfig`** - `version` (one-to-one, `PROTECT`), `organization` (denormalized from the template, enforced by trigger), scalar settings (`enabled`, `method`, `na_policy`, `missing_policy`, `precision`, `critical_overrides_grade`, `critical_label`), and one JSON **`document`**: `items` (rule per question code), `sections` (weights), `bands`, `critical`. It is declarative and strictly validated; nothing in it is ever evaluated as code. A single immutable row per version (instead of one table per concept) means a configuration is read, validated, sealed, cloned and compared as a whole and can never be half-applied (ADR 0073). History is kept with `django-simple-history`.
* **`ScoreResult`** - `execution`, `version`, `config` (+ `config_hash`), `algorithm`, `status` (`provisional` / `final` / `not_applicable`), `kind` (`submission` / `recalculation`), `is_current`, `earned`, `available`, `percentage`, `grade`, `label`, `display_label`, `critical`, `breakdown` (JSON: per question, per section, warnings, plain-language explanation, critical hits), `execution_revision`, `input_fingerprint` (SHA-256 of exactly the scored answers), `reason`, `created_by`. Constraints: one *current* result per execution (partial unique index), valid status, percentage 0-100, a `not_applicable` result has no percentage.

## Versioning (Phase 7 integration)

* Scoring belongs to a **checklist version** and is editable only while it is a **draft** (service check + database trigger). Publishing is `checklist.publish`; there is no separate publication of scoring.
* **Sealing:** the scoring configuration is part of the version's manifest (`apps.checklists.hooks.manifest_extensions`) and therefore of its SHA-256 `integrity_hash`. A version without scoring hashes exactly as it always did; any later change of a sealed configuration is detected by `verify_version_integrity`.
* **Publication validation** (`apps.checklists.hooks.publication_validators`): every error blocks publishing with the question/section named; warnings advise.
* **Clone:** a new draft starts with a copy of the previous version's scoring (`clone_hooks`); editing it never touches the published one. **Compare** shows scoring changes (settings, per-question rules, section weights, bands, critical rules).
* An execution is bound to the version it started on, so it is always scored by that version's rules, however many newer versions exist.

## Execution integration

* `apps.executions.hooks.after_submit` runs **inside the submission transaction** (after the execution became submitted and findings were detected). The scoring hook writes the one **final** `ScoreResult` under the execution's lock. A defect is caught, audited (`scoring.score_failed`) and never loses the submission; `manage.py score_executions` completes it (idempotent).
* While an execution is **in progress** it has no stored score. `services.score_of` computes a **provisional** score on demand from the same evaluated state (stored responses -> Phase 5 values -> Phase 6 evaluation); it is shown only to people with `scoring.recalculate`.
* **Offline:** device answers reach the server through the same `save_answers` / `submit` services (ADR 0057-0060), so they are validated, evaluated and scored by the server. A client-supplied score, grade or total is rejected by the strict sync and API schemas ("Unknown fields"). Replayed sync operations return the stored receipt and cannot create a second result.

## Security

Permissions (`apps/permissions/registry.py`): `scoring.view`, `scoring.configure` (organization-wide, they belong to a checklist), `scoring.score_view`, `scoring.breakdown_view`, `scoring.recalculate`, `scoring.finalize_recalculate` (location-scoped like the execution). **Seeing an execution never implies seeing its score or breakdown**; seeing a checklist never implies seeing or editing its scoring. Invisible/foreign objects are 404, forbidden ones 403, a published configuration 409. Database triggers (`guards.py`) freeze the configuration with its version and make results append-only; the admin is read-only.

## Known limitations

* No partial-credit or penalty (negative points) model; points are 0 or more.
* Weights are static numbers; there are no formulas or conditional weights (that belongs to Phase 16).
* A final score is never recomputed automatically when rules change - versions are immutable, so there is nothing to change. A recalculation exists for corrections of the *calculation* (an authorized, audited action that appends a result).
* Scores are not aggregated across executions or locations; reporting and dashboards are later phases. No findings, corrective actions or workflow transitions are created from a score: `ScoreResult` (and `services.score_of`) are the documented integration points.
