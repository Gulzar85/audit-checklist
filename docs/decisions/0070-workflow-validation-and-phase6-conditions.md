# 0070. Publication validation and Phase 6 conditions (no second expression engine)

- **Status:** Accepted
- **Date:** 2026-10-23

## Decision
* Publication runs a graph validation (one initial state, reachable success end, no dead ends / unreachable states, no exits from terminal states, loops only when allowed, branch-group rules) plus policy checks (handlers, assignee policies, approval modes, separation). Errors block publishing; warnings advise; the same report backs the designer, the API and the publish service.
* **Conditions reuse the Phase 6 operators and three-valued logic.** Workflow conditions compare *registered attributes* of the business record; the adapter turns a fact into a Phase 6 answer state and calls `evaluate_comparison`; AND/OR use `tri_and` / `tri_or`. Operator applicability comes from the Phase 6 comparison families. There is no parser and no evaluator of user text.
* **Fail closed:** UNKNOWN (missing value, deleted attribute, malformed node) never selects a branch and never lets a record start; a conditional action with no matching branch is refused (`no_branch`).
* Branches: several transitions may share (source, action); the first with a TRUE condition (an unconditional one is last) is taken - the engine never guesses.

## Consequences
Rule semantics are documented once (Phase 6). New operators or families appear in workflows automatically; new attributes are added by registering them with the target type.
