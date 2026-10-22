# 0058. A small, versioned client evaluator and a refuse-don't-degrade compatibility policy

- **Status:** Accepted
- **Date:** 2026-10-22

## Context
Offline use needs conditional behaviour on the device, but the Phase 6 engine is Python. Re-implementing all of it risks silent drift; omitting
rules risks letting an invalid checklist look complete.

## Decision
* The browser implements a **deliberately small subset** (`rules-engine.js`, `EVALUATOR_VERSION`) of the declarative rule semantics:
  three-valued logic, visibility to a fixed point, required/optional, comments, instructions and messages. It is held to the language-neutral
  `docs/rule-conformance-vectors.json` for everything it supports, in CI (`node --test`).
* The server decides what is supported (`apps/executions/offline/compat.py`). A checklist using anything else (text operators, date-time
  comparisons, option availability, validation constraints, evidence) is **not offered for offline preparation** and the reasons are shown.
  Nothing is silently omitted.
* Local validation mirrors the answer contract for feedback only. The server re-validates and re-evaluates at sync and submission.
* No build step: native ES modules, tested with Node's built-in runner and an in-memory storage double.

## Consequences
* Some checklists are online-only until the evaluator grows (bump `EVALUATOR_VERSION` and the vectors together).
* A package for a different evaluator version is refused by the client.
