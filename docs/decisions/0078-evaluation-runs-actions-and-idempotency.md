# 0078. Evaluation runs, controlled actions and idempotency

- **Status:** Accepted
- **Date:** 2026-10-24

## Decision
* **Provisional evaluations are computed on read and not stored** (score unavailable, no actions) - like scoring, so the answer-saving path gains no write. The **final run is one append-only `EvaluationRun`** written inside the submission transaction through `executions.hooks.after_submit` (after the score snapshot; executions import nothing from evaluation), in a savepoint: a defect is audited and never loses the submission; `evaluate_executions` completes it idempotently. Offline sync needs no special code (it submits through the same service).
* **Idempotency without blocking legitimate re-evaluations:** the run key hashes execution, rule-set hash, engine version, inputs fingerprint, execution revision and score result, so identical inputs return the existing run while changed inputs, a recalculated score or a different rule set append a new run (the previous is retired, kept). A partial unique index keeps one current run per execution.
* **Actions are a registry of handlers that call the owning service** and are **write-once records** with their own status. A *succeeded* action key (`eval:<execution>:<rule>:<index>`) is unique per organization, so re-evaluation never repeats a side effect; a duplicate workflow is *skipped*; a failure is that action's failure and never rolls back the evaluation, the submission or siblings. There are no external side effects in the transaction; later ones must use after-commit signals or a reliable-processing pattern.
* **Findings** get a new constrained source `evaluation` and `findings.services.create_from_evaluation` (owned by the Findings module, idempotent on the detection key, active organization classification only, no silent fallback). **Workflows** get a registered `execution` target (eligible only when submitted, no business actions). Corrective actions are not an action type; the contract would delegate to their lifecycle service.
* **Database guards:** runs are append-only (only `is_current` true -> false), action requests are write-once after `pending`, rule sets are frozen with their version; consistency triggers keep organization, version, execution and rule set aligned.
* **Re-evaluation** is an authorized, audited, reasoned action (`evaluation.reevaluate`); a recalculated score re-evaluates through `scoring.hooks.after_recalculate`.

## Consequences
A published rule change never rewrites history; repeated triggers are harmless; every side effect is traceable from a run to its finding or workflow, shown only to people allowed to see them.
