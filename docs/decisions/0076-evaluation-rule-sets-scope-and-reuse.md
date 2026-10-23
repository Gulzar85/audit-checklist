# 0076. Evaluation rule sets belong to the checklist version; Phase 6 and Phase 15 are reused, not duplicated

- **Status:** Accepted
- **Date:** 2026-10-24

## Decision
* **A rule set belongs to exactly one checklist version** (`EvaluationRuleSet`, one-to-one) and follows its lifecycle (draft -> published -> superseded). There is **no global or organization inheritance**: nothing implicitly overrides anything, the rule set used for an execution is always the one of its own version, and sharing rules is done by cloning (a copy). This is the simplest safe scope model that makes selection deterministic and history reproducible; a hierarchy can be added later without changing stored runs.
* **Same attachment mechanism as scoring** (ADR 0073): one validated JSON document per version, sealed in the integrity hash through `apps.checklists.hooks` (a version without rules hashes as before), validated at publication, cloned into drafts, compared, and frozen by a database trigger.
* **No second rule engine.** Comparisons are the Phase 6 operators, per-family applicability, `clean_operand` validation and missing-value semantics; the logic is the Phase 6 three-valued logic (only `tri_not` was added). Condition trees are bounded by construction (depth 4, 25 comparisons, 50 rules) and cannot contain cycles.
* **No second scoring engine.** The `score` source reads the stored current `ScoreResult`; a provisional, not-applicable or absent score is *unavailable* (UNKNOWN), never 0.
* **Fixed registries**: operators (Phase 6), input sources (`answer`, `score`, `execution`), actions (`request_finding`, `start_workflow`, `tag`). Nothing from configuration is ever executed or imported.

## Consequences
Rules are versioned, sealed and reproducible with their checklist; reuse across checklists is a copy; new input sources or actions are additions to a registry.
