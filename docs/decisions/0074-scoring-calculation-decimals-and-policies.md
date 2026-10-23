# 0074. One pure calculation: exact decimals, explicit N/A and missing policies, fail-explicit

- **Status:** Accepted
- **Date:** 2026-10-23

## Decision
* **`engine.calculate` is the only scoring implementation.** Pure (no database, clock or randomness); inputs are `ItemFacts` built from the Phase 6 evaluated state (or sample answers); output is a `ScoreOutcome` with the numbers, a per-question and per-section breakdown and plain-language explanations generated from the actual calculation. Views, serializers and templates only render it.
* **Exact `Decimal` arithmetic.** Numbers are canonical decimal strings in the configuration, `Decimal` in the engine; floats are refused at parsing. Points are kept at 4 places, the percentage is rounded **half-up** to the configured precision (0-4) - not banker's rounding, not binary floats.
* **Formulas** are documented per method (`percentage`, `raw_total`, `weighted_sections`); question weights act inside a section, section weights between sections, never twice. **No denominator -> `not_applicable`**, never 0 %.
* **N/A and missing are different policies.** N/A (both the `yes_no_na` answer and item-level N/A) follows `na_policy`; unanswered follows `missing_policy` **only when final** (a draft is provisional). Hidden / inactive questions are always excluded. An answer that cannot be mapped (a numeric value in no range, an unknown option) or is invalid is **excluded, flagged and keeps the result provisional** - it is never silently zero.
* **Only scorable types** (boolean, yes/no, yes/no/N-A, single and multiple choice, numeric, score) can carry a rule; everything else is never scored. No negative points (no penalties), no partial credit unless configured through `multiple_choice` aggregation.
* **Grades** come from bands over the rounded percentage (inclusive lower bound, lowest band at 0, unique minimums); the grade never replaces the number. **Critical rules** are flags beside the score; only an explicit configuration replaces the *displayed* outcome.
* **Deterministic ordering:** results follow the checklist's question order; storage key order (PostgreSQL `jsonb` reorders keys) never leaks into results or summaries.

## Consequences
Behavior is reproducible and explainable; every edge case has a documented outcome and a test. New methods or rule kinds are a change of `ALGORITHM_VERSION`.
