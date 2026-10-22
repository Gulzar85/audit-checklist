# 0056. Evidence boundary and hidden-answer retention

- **Status:** Accepted
- **Date:** 2026-10-22

## Context
Photo, file and signature answers need secure storage (Phase 11), and rules can hide a question after it was answered.

## Decision
* Until evidence storage exists, a non-empty photo / file / signature answer is refused (`evidence_unavailable`) - no file id,
  name or path is ever persisted, so fake evidence cannot exist. Optional media items may stay empty; required ones and
  rule-required evidence block submission with an explicit message, and the UI shows a notice instead of an upload control.
* Answers to items the rules hide are **retained while drafting** and ignored (not required, counted, validated or displayed); a
  client cannot create or edit them. At submission they are stamped `is_hidden`: kept for audit, excluded from views and from any
  future scoring.

## Consequences
* Checklists that require evidence cannot be completed until Phase 11 - deliberate and visible, not silent.
* No data is erased because a user changed an earlier answer, and the final record is unambiguous about which answers counted.
