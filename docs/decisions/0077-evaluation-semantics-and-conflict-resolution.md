# 0077. Evaluation semantics: UNKNOWN never acts, explicit order, severity precedence, generated explanations

- **Status:** Accepted
- **Date:** 2026-10-24

## Decision
* **UNKNOWN never triggers anything and is never FALSE.** A missing, empty, N/A, invalid, hidden or unavailable input yields UNKNOWN (except the presence operators on a question); `AND`/`OR` are Kleene; `NOT` keeps UNKNOWN. A rule's `on_unknown` is `skip` (recorded as undecided) or `indeterminate`. Missing inputs are never converted to zero or `False`; a rule that wants to handle absence says so with `IS_EMPTY` or `score.status`.
* **Deterministic order:** rules run by `(priority, code)`; equal priorities are a publication error; `stop` ends processing and records the rest as skipped; disabled rules never run.
* **One outcome set** (`PASS`, `WARNING`, `REVIEW_REQUIRED`, `FAIL`, `INDETERMINATE`, `NOT_APPLICABLE`) separate from every domain status, aggregated by a fixed precedence (`FAIL > REVIEW_REQUIRED > INDETERMINATE > WARNING > PASS`). Every matched rule is kept: a critical match is never discarded, and a definite failure is never softened by an undecided rule. No enabled rule is `NOT_APPLICABLE` (never an invented pass).
* **Explanations are generated from the evaluation itself** - the rule's configuration and the observed values - never composed afterwards; the per-condition tree with each result is stored on the run.
* **Observed values are protected:** the summary (outcome, matched rule names) needs `evaluation.result_view`; conditions, observed values and action outcomes need `evaluation.explain`.

## Consequences
Rule authors can reason about every case with one table; results are reproducible; engine changes that alter results bump `ENGINE_VERSION`, stored on every run.
