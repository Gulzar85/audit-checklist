# Corrective actions architecture (Phase 13)

A **corrective action** is an independently traceable unit of work linked to one finding. Assignment, progress, evidence, verification
and closure are governed by explicit rules and audited transitions - never by an unrestricted status update, and never by the assumption
that completed work is verified.

## Domain model (`apps/corrective_actions`)

| Model | Purpose | Key integrity |
|---|---|---|
| `CorrectiveAction` | the unit of work: title, description, structured **plan** (`plan`, `expected_outcome`, `completion_criteria`), `priority`, `status`, `assigned_to`, `due_at`, evidence requirement, submission/verification/closure stamps | `finding` (PROTECT); organization and location are the finding's (trigger); unique `(organization, number)` (`CA-00042`); status/lifecycle/priority checks; never deleted (trigger) |
| `CorrectiveActionUpdate` | the **append-only work log**: progress, blocker, blocker cleared, submission, correction | a correction points at an update of the same action; no update/delete (trigger); optional 0-100 `progress_percent` |
| `CorrectiveActionVerification` | the **append-only decision** (approved / rejected) of one submission: verifier, notes, rejection reason, time | unique `(action, submission_number)`; rejection needs a reason; insertable only for a *submitted* action with that submission number (trigger); no update/delete |

* **Many actions per finding** (a real foreign key, never a JSON array); each has its own owner, due date, status, log, evidence, decisions and history.
* **No duplicated state**: organization and location are copied from the finding once (immutable, trigger-checked) so scope checks and indexes need no join.
* **Policy snapshots**: `independent_verification` and `required_evidence` are copied onto the action at creation, so a later settings change never alters work already under way.
* `django-simple-history` records every change of an action; the activity log carries the semantic events (below).
* Evidence is attached through the Phase 11 registry (`EvidenceAttachment.corrective_action` + `purpose`); nothing is copied.

## Layering
`models` -> `services/` (`lifecycle`: create, update, assign, work log, transitions - the only writers) -> `selectors` (authorized reads) ->
`policies` (permissions, ownership, separation of duties) -> `eligibility` (the parent-finding policy) -> `views`/`forms`/`filters`/`tables`
(web), `apps/api/v1/corrective_actions.py` (API), `admin.py` (read-only). Web and API share the same services and policies.
`integration.py` registers the evidence target and the finding hooks at startup; Phases 11 and 12 do not import this app.

## Audit events
`corrective_action.created|issued|started|blocked|resumed|submitted|verified|rejected|closed|cancelled|reopened`, `.updated` (before/after),
`.due_date_changed`, `.priority_changed`, `.assigned` (before/after), `.progress_added`, `.transition_refused`, plus the Phase 11 `evidence.uploaded|attached|detached|downloaded`
entries (tagged with `action_id`). Every entry carries `action_id` and `finding_id`, so the action's and the finding's timelines both find it.
No evidence content is logged.

## Retention
Actions, updates and verifications are never deleted; a finding with actions cannot be deleted (`PROTECT`); users becoming inactive leave the records intact
(`SET_NULL`). Cancelling an action keeps it for the record.

## Integration points for the future Workflow Engine (Phase 14)
`events.py` sends after-commit signals - `action_created`, `action_issued`, `action_assigned`, `action_updated`, `action_progress_added`, `action_started`,
`action_blocked`, `action_resumed`, `action_submitted`, `action_rejected`, `action_verified`, `action_closed`, `action_cancelled`, `action_reopened`
(and a reserved `action_became_overdue`: overdue is *derived*, so a later scheduled job raises it). A subscriber must call the lifecycle services; it can never write
`status`. There is no outbox: signals are in-process and the activity log is the durable record; guaranteed delivery is a Phase 14 decision. A multi-stage approval
would add further decision rows (the verification table is per submission and append-only) - the extension point exists, the engine does not.

## Testing and troubleshooting
```bash
python -m pytest tests/test_actions_lifecycle.py tests/test_actions_api.py tests/test_actions_web.py tests/test_actions_commands.py
```
| Symptom | Likely cause |
|---|---|
| "Assign a responsible user first" on issue/start/submit | the action has no owner |
| "N verified completion file(s) are required" | `required_evidence` > files that are *available* (pending or quarantined files do not count) |
| Verify/Reject buttons missing | you own or submitted the action (separation of duties), lack `corrective_action.verify`/`reject` at the location, or it is not submitted |
| A finding cannot be resolved | an action is outstanding (see [corrective-action-finding-policy.md](corrective-action-finding-policy.md)) |
| An owner cannot be chosen | owners must be active members holding `corrective_action.start` at the finding's location |

## Known limitations
* One verification stage (approve/reject); no multi-stage approval.
* Due dates are timestamps; there are no reminders, escalations or recurring jobs (derived `overdue` is available to reports).
* No bulk creation (no business need was established); creation is one action at a time.
* The progress percentage is information only. Evidence is attached to the action with a stage (`purpose`), not to individual log entries.
* Verification of a *reopened* action requires a fresh submission; earlier decisions stay in the history.
* A verified action can be reopened (with a reason) by someone holding `corrective_action.reopen`; this never changes the finding.
