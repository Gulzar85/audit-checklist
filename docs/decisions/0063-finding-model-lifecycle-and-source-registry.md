# 0063. Findings: explicit-key model, controlled sources, and a lifecycle where "resolved" is not "verified"

- **Status:** Accepted
- **Date:** 2026-10-23

## Context
Phase 12 needs a record that survives template changes, is secure per organization/location, and can later be tied to corrective
actions and verification - without a client-controlled way to point a finding at arbitrary data and without pretending something was verified.

## Decision
* **One `Finding` model with explicit `PROTECT` foreign keys** (organization, location, assignment, execution, exact version, item, response). No `GenericForeignKey`; the source is
  a value of the controlled `SourceType` registry (`checklist_rule`, `manual`). A new source is a deliberate extension (new choice + validating service).
* **Configuration is data**: `FindingCategory` and `FindingSeverity` per organization. Severity is compared by integer `rank`, never by text; defaults are seeded lazily and are
  ordinary rows an administrator may change (codes are permanent, nothing is deleted).
* **Integrity in the database**: a trigger enforces organization consistency, source consistency, immutability of traceability and no deletion; constraints enforce status,
  lifecycle stamps and the rule-source completeness. Services check first; the database is the last line.
* **Lifecycle** `open -> acknowledged -> in_progress -> resolved -> closed`, plus `reopened` and `cancelled`, as a transition table (`constants.TRANSITIONS`) used by one service.
  **Resolved means a fix was reported; closed means an authorized person accepted it.** There is no `verified` flag. Closure runs registered blockers and, when
  `FINDINGS_REQUIRE_VERIFICATION` is on, requires a verification gate - which does not exist yet, so closing is refused rather than faked.
* **Cancelled is final**; a cancelled rule-raised finding is not recreated. Reopen is allowed from resolved/closed with a reason.
* Permissions extend the registry (`finding.acknowledge|assign|resolve|reopen|cancel` location-scoped; `finding.manage` organization-wide); history is visible with `finding.view`.

## Consequences
* Phase 13 (corrective actions) attaches through `hooks.closure_blockers` and the after-commit signals without changing this module.
* Extending sources, statuses or severities is a reviewed code/data change, never a client request.
* A finding can be audited end to end: version, item, answer, rule, who, when, and every transition.
