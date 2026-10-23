# Triggers, idempotency, actions, history, security limits and operations

## When an evaluation happens

| Trigger | What | Stored? | Actions? |
|---|---|---|---|
| **Submission** (online or offline) | `executions.hooks.after_submit` runs the evaluation inside the submission transaction, after the score snapshot, under the execution lock | yes, the *final* run | yes |
| **Score recalculated** | `scoring.hooks.after_recalculate` evaluates again with the new current score | a new run if inputs changed | only new ones |
| **Re-evaluation** | `POST /executions/{id}/evaluation/evaluate/` (`evaluation.reevaluate` + a reason) on a submitted execution | a new run if anything changed | only new ones |
| **Provisional** | an execution in progress, on read (`evaluation.evaluate`) | **no** | **never** |
| **Retry** | `manage.py evaluate_executions` for submitted executions whose submission-time evaluation did not complete | as above | as above |

A defect in the submission-time evaluation is caught, audited (`evaluation.run_failed`) and **never loses the submission**. Offline answers are not trusted as results: the device synchronizes answers; the server validates them (Phase 5), applies the rules (Phase 6), stores them, and evaluates at submission. The sync schema rejects unknown fields, so a payload containing `outcome` or `evaluation` is refused.

## Idempotency

* **Run key** = `sha256(execution, rule-set hash, engine version, inputs fingerprint, execution revision, score result id)` (unique). The same inputs under the same rules **return the existing run**: no new row, no action, an `evaluation.run_unchanged` audit entry. Changed answers, a different score, or a different rule set create a new run, retire the previous one (kept) and record the trigger and reason.
* **Action key** = `eval:<execution>:<rule>:<action index>`. A *succeeded* key is unique per organization (partial unique index), and a new run finding the key already succeeded records the action as **skipped** ("already performed by an earlier evaluation run"). So re-evaluating never raises a second finding or starts a second workflow for the same rule and execution. A *failed* action is not retried silently; it can be retried only by a re-evaluation whose inputs changed.
* Concurrency: the execution row is locked, the run key and `is_current` have unique indexes, finding creation locks the organization and recovers from a lost race on its detection key; threaded tests cover simultaneous submissions and re-evaluations.

## Actions and the domain contracts

* **`request_finding`** -> `findings.services.create_from_evaluation(execution, rule_code, rule_name, detection_key, category_code, severity_code, title, description, requires_review)`: creates a finding with source `evaluation` (new, constrained: assignment, execution, version, detection key and rule code are required), location = the assignment's location, classification only from the organization's **active** category/severity (no silent fallback). Returns the existing finding for a known key. It never changes a finding's status.
* **`start_workflow`** -> `workflows.services.start(target_type="execution", target=execution, definition_code=..., system=True)`; the engine enforces eligibility (only a submitted execution), the published version and `already_running` (recorded as *skipped*). The workflow's tasks are completed by people through the workflow engine; the evaluation never moves a state or assigns a task. The `execution` target has no business actions (an execution has no lifecycle decision a workflow could take over).
* **`tag`** -> recorded on the run for later modules (notifications, reporting).
* **Corrective actions:** no action type exists; a future requirement would add a handler delegating to `apps.corrective_actions.services` - never a direct model write.
* Each action runs in its own savepoint after the run is recorded; **an action failure is recorded as that action's failure** (`evaluation.action_failed`) and never rolls back the evaluation, the submission or a sibling action. There are no external side effects inside the transaction in this phase; a future one must use after-commit signals or a reliable-processing pattern.

## History and audit

Runs are append-only (PostgreSQL triggers: no delete; only `is_current` true -> false), action requests are write-once after `pending`, rule sets are frozen with their version. Audit verbs: `evaluation.ruleset_saved|removed|cloned`, `run_recorded`, `run_unchanged`, `run_failed`, `action_succeeded|skipped|failed`; the activity entry carries the execution, rule-set hash, engine version, trigger, matched rules, action statuses and reason - never answer values or evidence content.

## Security limits and safe configuration

Fixed registries for operators, sources and actions; no `eval`/`exec`, no SQL, no JavaScript, no dynamic import or callable, no model-field traversal; unknown keys are errors. Limits: 50 rules, 25 comparisons per rule, depth 4, 5 actions, 300-character messages, operand limits from Phase 6. Conditions are trees by construction (no references), so cycles cannot exist. Reference data (categories, severities, workflows) is organization-scoped and re-checked when an action runs. Cross-organization references are impossible: the finding service and the workflow engine resolve everything through the execution's own organization.

## Operations

```bash
python manage.py evaluate_executions --dry-run   # submitted executions that have no evaluation yet
python manage.py evaluate_executions             # create the missing ones (idempotent; never repeats an action)
python manage.py migrate && python manage.py seed_permissions && python manage.py seed_roles
python -m pytest tests/test_evaluation_engine.py tests/test_evaluation_integration.py tests/test_evaluation_api.py tests/test_evaluation_web.py
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "These rules cannot be published yet" | open **Evaluation**; each message names the rule and the problem |
| An execution shows no evaluation | its version has no rules or they are switched off ("not evaluated"), or the submission-time evaluation failed - see `evaluation.run_failed`, then `evaluate_executions` |
| A rule is *undecided* | an input it reads was missing / N/A / hidden / unavailable (the explanation names it); use `IS_EMPTY` or `score.status` to handle that case explicitly |
| An action *failed* | the category / severity / workflow was deactivated after publication, or the workflow had no eligible assignee; see the action's error |
| 403 on the evaluation although the execution is visible | `evaluation.result_view` / `explain` are separate permissions |
| A new rule version did not change an old result | by design: results are bound to the version that produced them |
