# 0047. Conditional rules: declarative, versioned, validated as a set, evaluated by one pure function

- **Status:** Accepted
- **Date:** 2026-10-16
- **Extends:** [0045](0045-generic-checklist-definition-model.md), [0046](0046-answer-type-engine.md)

## Decision
* A rule is **data**: a condition tree (AND/OR groups of comparisons) and a list of actions, stored as JSON on
  `ChecklistRule` and referencing questions and sections by their stable **codes** within one checklist version. Rules are
  versioned with the checklist: editable in the draft, frozen when published, copied into the next draft.
* Operators and actions are **registries** (data + a small pure function), not code paths: which operators a question offers
  follows its answer type's `comparison_family`; actions declare target, parameters and whether they are *graph edges*. No
  `eval`, no expression language, no regular expressions; unknown operators/actions/keys are rejected.
* Rule values and constraints reuse the **Phase 5 engine** - a comparison value is parsed with the source answer type, a
  validation constraint is a *tightening* of the type's own configuration schema - so there is no second parser.
* Conditions use **three-valued logic**: an unanswered, N/A, invalid or hidden source gives UNKNOWN, never FALSE, and only
  TRUE acts. This makes "not answered" distinct from "No" and keeps follow-ups hidden until their trigger exists.
* **One validator, one evaluator.** `validate_rule_set` is the only gate (references, operators, values, limits, duplicate
  codes, dependency **cycles**, definite conflicts); services call it on create/update/enable, on structure changes that
  would break a rule, and at publish. `evaluate` is a pure function of a DB-free `VersionSnapshot`, a compiled rule set and
  the answers, so the builder, API, import, admin, a future execution phase and a future offline PWA share it.
* **Deterministic conflict precedence:** hide beats show, required beats optional, options intersect, constraints take the
  tightest value, messages accumulate in rule order. Definite conflicts are save-time errors; possible ones are warnings that
  name the winner.
* **Hidden is not deleted.** The engine computes an effective state and never mutates answers; the server is the source of
  truth and a client's local evaluation is advisory (language-neutral conformance vectors pin the behaviour).
* The builder's rule editor is **server-driven HTMX** (the whole editor is posted, the server applies one operation and
  re-validates) rather than client-side state, keeping validation single-sourced and the CSP unchanged; JSON is available
  only as an advanced view that goes through the same service.
* Everything is bounded by configurable **limits** (`CHECKLIST_RULE_LIMITS`), enforced by validators, and the simulator and
  API inputs are size-bounded and never persist anything.

## Consequences
* Adding an operator or action is a registry entry (+ docs and vectors); the builder, API metadata and validation follow.
* Renaming or retyping a question that a rule uses is refused until the rule is fixed; codes are therefore stable identifiers
  and the section gained a `code`.
* A rule cannot read other checklists, repetitions, computed values or the clock: evaluation is a pure function of answers.
* The visual editor shows a root group plus one nested level; deeper rules (up to the configured depth) are edited as JSON.
* Evidence and comment rules are *requirements the engine reports*; storage and enforcement of files belongs to later phases.

## Alternatives considered
* **Rules as code or an expression string** - rejected: unsafe to evaluate, impossible to validate and to explain in the UI.
* **Per-answer-type rule tables** - rejected for the same reason ADR 0046 rejected per-type tables.
* **Two-valued logic (missing = FALSE)** - rejected: `NOT_EQUALS` would fire on empty answers and reveal follow-ups too early.
* **Client-side builder state** - rejected for now: a second validation path to keep in sync, and a heavier CSP-safe JS bundle.
