# Corrective action lifecycle, ownership, due dates and verification

Statuses: `draft`, `open`, `in_progress`, `blocked`, `submitted` (shown as *awaiting verification*), `rejected`, `verified`, `closed`, `cancelled`.
**Overdue is derived**, never a status. Only `services.lifecycle.transition` changes `status`.

## Transition matrix

| Action | From | To | Permission (at the location) | Who | Input |
|---|---|---|---|---|---|
| `issue` | draft | open | `corrective_action.create` | manager | needs an owner |
| `start` | open | in_progress | `corrective_action.start` | owner or manager* | - |
| `block` | in_progress | blocked | `corrective_action.block` | owner or manager* | **blocker text** |
| `resume` | blocked, rejected | in_progress | `corrective_action.start` | owner or manager* | optional note (clears the blocker) |
| `submit` | in_progress, blocked | submitted | `corrective_action.submit` | owner or manager* | **completion summary**; evidence requirement met |
| `verify` | submitted | verified | `corrective_action.verify` | verifier (separation of duties) | optional notes |
| `reject` | submitted | rejected | `corrective_action.reject` | verifier (separation of duties) | **reason** |
| `close` | verified | closed | `corrective_action.close` | manager | - |
| `cancel` | draft, open, in_progress, blocked, rejected | cancelled | `corrective_action.cancel` | manager | **reason** |
| `reopen` | verified, closed | in_progress | `corrective_action.reopen` | manager | **reason** |

\* "owner or manager": the responsible user, or someone holding `corrective_action.update` at the location (acting for the owner) - in both cases the specific permission is still required.

Order of checks: authorization incl. ownership/separation (403) -> already in the requested state (409, nothing changed) -> invalid move (400) -> required text, owner,
evidence (400) -> one locked write with history, audit and an after-commit signal. A refused request is audited (`corrective_action.transition_refused`). Concurrency: the action row is locked,
so two simultaneous decisions on one submission produce exactly one decision (also enforced by `UNIQUE(action, submission_number)`).

**Completion, verification and closure are three events.** `submit` reports the work as done; `verify` records that someone *else* checked it; `close` records the administrative end.
Verified/closed never change the parent finding (see the finding policy). **Cancelled is final.** A cancelled action cannot continue.

## Rejection, correction, resubmission
`reject` returns the action to the owner (`rejected`); the owner `resume`s (-> in progress), corrects, and `submit`s again - a new submission number, a new decision.
Every decision stays in `CorrectiveActionVerification` (append-only). Reopening a verified/closed action clears its `verified_*`/`closed_*` stamps and counts (`reopen_count`) but keeps the old decisions.

## Ownership and reassignment
* An owner must be an **active member** of the organization holding `corrective_action.start` at the finding's location; users of another organization are refused (no cross-organization assignment).
* The creator may assign themselves if they are eligible (same rule for everyone).
* First assignment needs `corrective_action.assign`; changing an existing owner needs `corrective_action.reassign`. **The owner cannot hand their own work away** (managers reassign).
* Clearing the owner is only possible while draft/open; work under way can only be reassigned. All changes are audited with before/after.

## Planning edits
A manager (`corrective_action.update`) edits title, description, plan, expected outcome, completion criteria, priority, due date and the evidence requirement; the **responsible user edits the plan text only**
(they cannot move their own deadline or priority). Editing is possible only while planning (draft, open, in progress, blocked, rejected) - a submitted, verified, closed or cancelled action is locked (409).

## Due dates and overdue
`due_at` is a timezone-aware **timestamp**; forms take a local date-time (must be in the future); APIs use ISO 8601. An action is **overdue** when it has a due date in the past **and** the owner still has work
(`open`, `in_progress`, `blocked`, `rejected`). `submitted` is excluded (the owner's part is done; delay there is the verifier's), as are verified/closed/cancelled. It is computed on every read and as a query
filter (`selectors.overdue`, `?status=overdue`); no flag is stored and no job is needed for correctness.

## Progress updates and blockers
Owners (or acting managers) append progress notes while work is in progress or blocked: note text, optional 0-100 percent, optionally correcting an earlier entry. Entries are append-only (database trigger) - a
correction is a *new* entry pointing at the old one. The percentage never changes the status and 100% is not verification. Reporting a blocker moves the action to `blocked` and logs it; resuming logs "blocker cleared".

## Verification and separation of duties
* The verifier needs `corrective_action.verify` (approve) or `corrective_action.reject` at the location.
* Where the action requires independent verification (default; snapshot of `CORRECTIVE_ACTION_INDEPENDENT_VERIFICATION`), the verifier is **neither the owner nor the submitter**; `CORRECTIVE_ACTION_VERIFIER_NOT_CREATOR` additionally excludes the creator. These users do not see their own work in the verification queue and receive 403 on a direct request.
* A decision requires a submitted action (service and database trigger); the verifier is always the authenticated user - a client can send neither a verifier nor a decision value.
* A rejection requires a reason; the decision, notes, reason, verifier and time are preserved forever.

## Evidence stages (`purpose`)
| Purpose | Attached by | While the action is |
|---|---|---|
| `plan` | owner / manager | draft, open, in progress, blocked, rejected |
| `progress` | owner / manager | in progress, blocked |
| `completion` | owner / manager | in progress, blocked - **counts towards `required_evidence`** |
| `verification` | verifier | submitted |
| `rejection` | verifier | submitted |
Only `available` (scanned) files count; pending and quarantined files are listed with their state and never served or counted. Every view re-checks access ([evidence-attachment-targets.md](evidence-attachment-targets.md)).
