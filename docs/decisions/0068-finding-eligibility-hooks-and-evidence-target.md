# 0068. Parent-finding eligibility through hooks, and the `corrective_action` evidence target

- **Status:** Accepted
- **Date:** 2026-10-23

## Decision
* **The finding is never changed by an action.** Eligibility ("no outstanding actions") is a fact computed by `eligibility.finding_eligibility`. It is applied through hooks the Phase 12 service already
  documents and that Phase 13 extends with `resolve_blockers` and `cancel_blockers` (and uses `verification_gate`): the *finding* service refuses to resolve/cancel while actions are outstanding, and a person still
  performs every finding transition. The corrective-action app registers itself at startup; Phases 11 and 12 do not import it. Resolution blocking can be disabled by setting.
* **Evidence reuses Phase 11** by adding a target type, as ADR 0060 prescribed: an explicit nullable `corrective_action` key and a `purpose` on `EvidenceAttachment`, a check constraint, a unique live attachment per
  (file, action), trigger updates (organization consistency, immutability) and a `TargetSpec`. To support it the evidence services were generalised *behaviour-preservingly*: instead of calling the execution
  helpers directly they call the target's `lock`, `touch` and `audit_meta` and filter on `ctx.key`. The existing checklist-response target implements them with its previous behaviour (all Phase 11 tests unchanged).
* Stage rules are the target's: `purpose` decides who may attach and in which status (plan/progress/completion by the worker; verification/rejection by the verifier), checked under the action row lock. Only
  `available` files count towards `required_evidence`.

## Consequences
* A later target (finding evidence, ADR 0065) is now a registry entry plus a key - no further service change.
* Evidence is attached to the action with a stage, not to individual log entries (documented limitation).
