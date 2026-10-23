# Automated evaluation architecture (Phase 16)

The evaluation engine judges a **submitted checklist execution** against versioned, declarative rules and produces an explainable outcome plus controlled action requests. It reads the Phase 6 answer states and the Phase 15 stored score; it does not validate answers, decide visibility, calculate a score, or own findings, corrective actions or workflows - it asks the owning services.

## Components (`apps/evaluation`)

| Module | Responsibility |
|---|---|
| `constants.py` | vocabularies: outcomes + precedence, rule statuses, triggers, action types, limits, `ENGINE_VERSION` |
| `sources.py` | the fixed **input-source registry** (`answer`, `score`, `execution`), typed operand parsing, the states a run reads |
| `config.py` | the rule schema: strict parsing, compilation against the version's questions, validation, canonical form and hash. Pure |
| `engine.py` | **`evaluate(rules, inputs)`**: the deterministic interpreter (three-valued logic, ordering, conflicts, explanations). Pure |
| `actions.py` | the controlled **action registry** (finding request, workflow start, tag) |
| `models.py`, `guards.py` | `EvaluationRuleSet`, `EvaluationRun`, `EvaluationActionRequest` and their PostgreSQL triggers |
| `services/configuration.py` | writing/validating a draft's rules; the hooks the version lifecycle calls |
| `services/runtime.py` | provisional evaluation, the final stored run, re-evaluation, the designer's what-if |
| `policies.py`, `selectors.py` | authorization and scoped reads |
| `integration.py` | registers with `apps.checklists.hooks`, `apps.executions.hooks` and `apps.scoring.hooks` - none of them imports evaluation |
| `views.py`, `templates/evaluation/`, `apps/api/v1/evaluation.py` | editor, rule form (condition builder), preview, result pages, REST API |

Web UI, REST API, offline synchronization and the tests all use the same `engine.evaluate` and the same services.

## What it reuses (no second engine)

* **Phase 6** operators (`OPERATORS`, per-family applicability, `clean_operand`, `evaluate_comparison`, missing-value semantics), `AnswerState`, and the three-valued logic. Only `tri_not` was added (additive). Answer comparison values are validated by the question's own answer type.
* **Phase 15**: the stored current `ScoreResult` is the only source of score values. No score arithmetic exists in this app.
* **Phase 12**: `findings.services.create_from_evaluation` (new, owned by the Findings module, new source `evaluation`) - idempotent on its detection key.
* **Phase 14**: `workflows.services.start` for the newly registered `execution` target.
* **Phase 7**: the version lifecycle (manifest seal, publication validation, clone, compare) through hooks; **Phase 9/10**: `after_submit`, which offline sync reaches through `services.submit`.

## Scope model (deliberately simple)

A rule set belongs to exactly **one checklist version** and an execution is always evaluated with the rule set of **its own version**. There is no global or organization inheritance: nothing implicitly overrides anything, selection is deterministic, and an organization that wants the same rules on every checklist clones them into each version (a copy, never a link). Retired = the version is superseded; its rules and every evaluation made with them stay available.

## Data model

* **`EvaluationRuleSet`** - `version` (one-to-one, `PROTECT`), `organization`, `enabled`, `document` (`{"rules": [...]}`), history. Editable only while the version is a draft; frozen by a trigger and sealed in the version's integrity hash once published.
* **`EvaluationRun`** - append-only: execution, exact version and rule-set hash, engine version, trigger, outcome, `is_current`, execution revision, the `score_result` read, the inputs' fingerprint, `idempotency_key` (unique), reason, initiator (null = the system), and `detail` (per-rule results, per-condition results, explanations, undecided rules).
* **`EvaluationActionRequest`** - one requested action with its own status (`pending|succeeded|skipped|failed`), result reference (finding / workflow / tag), error. Write-once after `pending`; a **succeeded** `action_key` is unique per organization.

A run is not the execution's status, a finding's status, a corrective action's status or a workflow's status, and it never completes or closes anything.

## Security

Permissions `evaluation.view` / `evaluation.configure` (organization-wide) and `evaluation.result_view` / `explain` / `evaluate` / `reevaluate` (location-scoped, plus visibility of the execution). Seeing an execution never implies seeing its evaluation; the observed values live behind `explain`. No arbitrary code, SQL, JavaScript, dynamic import or ORM path exists anywhere: operators, sources and actions are fixed registries; a malformed rule is an error, never executed. Limits: 50 rules, 25 comparisons per rule, depth 4, 5 actions per rule. Details in [evaluation-integrations-and-idempotency.md](evaluation-integrations-and-idempotency.md).

## Known limitations

* Rule sets do not inherit or override across versions, checklists or organizations; reuse is by cloning.
* Only `answer`, `score` and `execution` inputs exist; adding a source means adding it to `sources.py` (a fixed adapter registry), nothing else.
* Actions: findings, workflow start and tags. Corrective-action automation is not implemented (a future contract would delegate to `apps.corrective_actions.services`); notifications and scheduling are Phases 17-18.
* Provisional evaluations are computed on read and not stored; the score is unavailable until submission.
* Section/question-level score summaries are not exposed as inputs yet (only the overall result).
