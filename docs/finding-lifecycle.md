# Finding lifecycle

Statuses: `open`, `acknowledged`, `in_progress`, `resolved`, `closed`, `reopened`, `cancelled`. Only `services.lifecycle.transition`
changes `status`; no serializer, form, import or admin can.

## Transition matrix

| Action | From | To | Permission (at the finding's location) | Required input |
|---|---|---|---|---|
| `acknowledge` | open, reopened | acknowledged | `finding.acknowledge` | - |
| `start` | open, acknowledged, reopened | in_progress | `finding.resolve` | a responsible user when the severity `requires_owner` |
| `resolve` | open, acknowledged, in_progress, reopened | resolved | `finding.resolve` | **resolution summary** (<= 1000) |
| `close` | resolved | closed | `finding.close` | - (closure policy below) |
| `reopen` | resolved, closed | reopened | `finding.reopen` | **reason** (<= 300) |
| `cancel` | open, acknowledged, in_progress, reopened, resolved | cancelled | `finding.cancel` | **reason** (<= 300) |

Edits (`finding.update`: title, description, category, severity, due date) and `assign` (`finding.assign`: responsible user, due date)
are allowed while the finding is not closed/cancelled. Source traceability, number, detection data and creator are never editable.

Order of checks: authorization (403) -> already in the requested state (409, nothing changed) -> invalid move (400) -> required text
(400) -> action-specific rules -> one locked write with history and audit. A refusal is itself audited (`finding.transition_refused`).

## Resolved is not verified
`resolved` means *the fix was reported*; nobody has independently verified it, and the UI says so. `closed` means an authorized
person accepted the resolution (and any policy below passed). There is no `verified` field or status in Phase 12.

## Closure policy and the Phase 13 integration point (`hooks.py`)
* `hooks.register_closure_blocker(fn)` - `fn(finding) -> str | None`; a message blocks `close` (e.g. "2 corrective actions are still open").
* `FINDINGS_REQUIRE_VERIFICATION=true` makes closing require `hooks.register_verification_gate(fn)` to accept the finding. Until a
  verification phase registers a gate, closing is **refused** (`closure_blocked`) rather than pretending the finding was verified.
  The default is `false`: closing is then an authorized human decision.
* Signals after commit (`events.py`): `finding_raised`, `finding_status_changed`, `finding_assigned`, `finding_updated` - no receivers yet.

## Other rules
* **Cancelled is final**: no action leaves it; a cancelled rule-raised finding is not recreated by a later detection run.
* **Reopen** keeps the previous resolution in the audit entry (`previous_resolution`), clears the resolved/closed stamps and counts (`reopen_count`).
* **Overdue** is derived (`due_at` past and still `open`/`acknowledged`/`in_progress`/`reopened`); it is never stored, and finished work is never overdue.
* **Owner policy**: assignees must be active members who hold `finding.resolve` at the location. A severity with `requires_owner`
  needs an owner at manual creation and before work starts; rule-raised findings start unassigned and flagged for assignment.
