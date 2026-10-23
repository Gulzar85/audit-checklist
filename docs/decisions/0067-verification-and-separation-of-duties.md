# 0067. Verification and separation of duties

- **Status:** Accepted
- **Date:** 2026-10-23

## Decision
* Verification is an **explicit decision record** (approved / rejected) made by the authenticated verifier - never client-supplied - with a mandatory reason for rejection, one per submission cycle.
* **Separation of duties is a policy function** (`policies.separation_ok`) used by the transition check, the verification queue and the UI: where the action requires independent verification (default),
  the owner and the submitter can neither approve nor reject, and `CORRECTIVE_ACTION_VERIFIER_NOT_CREATOR` extends this to the creator. The requirement is **snapshotted** on the action at creation.
  A deployment that does not need independence can switch it off (`CORRECTIVE_ACTION_INDEPENDENT_VERIFICATION=false`); the default is the safe one.
* A rejected action returns to its owner (`resume` -> `submit` again) and every earlier decision stays in history. A verified action may be reopened by someone holding `corrective_action.reopen`.
* A second verifier / multi-stage approval is *not* built; the decision table is per submission and append-only, which is the extension point for a Phase 14 workflow.

## Consequences
* A single-user deployment cannot verify its own work - by design; the demo command leaves the action awaiting verification instead of faking independence.
* Direct requests by the owner get 403 even though the endpoint exists; the queue simply never lists their own work.
