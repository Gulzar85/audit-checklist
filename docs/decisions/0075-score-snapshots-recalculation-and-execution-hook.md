# 0075. Score snapshots, controlled recalculation and the submission hook

- **Status:** Accepted
- **Date:** 2026-10-23

## Decision
* **Provisional scores are computed on read, not stored.** An in-progress execution has no `ScoreResult`; avoiding a write inside the answer-saving hot path keeps saves fast and means a stale provisional value can never exist. Previews are shown only to those with `scoring.recalculate`.
* **The final score is one append-only `ScoreResult` written inside the submission transaction**, through `apps.executions.hooks.after_submit` (executions do not import scoring). It runs after findings detection under the execution lock, in a savepoint: a scoring defect is audited and **never loses a submission**; `score_executions` completes it later, idempotently.
* **Append-only in the database:** triggers forbid deletes and any update except retiring `is_current`; a partial unique index allows one current result per execution; a new result must agree with its execution, version and configuration.
* **Controlled recalculation** (`scoring.finalize_recalculate` + a reason) recomputes under the version's frozen configuration. Identical fingerprint, configuration hash and algorithm -> nothing changes; otherwise a new result is appended and the previous one retired and kept. No endpoint or screen edits a result.
* **Reproducibility metadata** on every result: algorithm version, configuration hash, execution revision, input fingerprint.
* **Offline** needs no special code: device answers reach `save_answers` / `submit`; the server evaluates and scores them; the sync schema rejects client-supplied scores.
* **Authorization:** summary, breakdown, preview and final recalculation are separate permissions at the execution's location; visibility of the execution is necessary but never sufficient.

## Consequences
A final score cannot silently change; history is complete; the only integration points for Phase 16 are `ScoreResult` and `services.score_of`. Findings, corrective actions and workflows are not created or moved by a score in this phase.
