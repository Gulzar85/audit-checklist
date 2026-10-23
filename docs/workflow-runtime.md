# Workflow runtime: starting, transitions, tasks, approvals, separation of duties, domain integration

## Starting a workflow (`engine.start`)
The record is resolved through the **target registry** (`targets.py`): the client names a registered `target_type` and an id; the record is loaded through that module's *authorized selector* (an invisible or foreign record is
"not found"); organization and location come from the record. The service checks `workflow.start` at the location (domain-event starts are system starts), the record's eligibility, resolves the workflow (explicit code, else the
organization default, else the system default), evaluates the version's start condition (anything but TRUE refuses), refuses a second active instance of the same definition, and in one transaction creates the instance on the **published
version**, its start event and the initial state's tasks. **Auto-start**: a definition with an `auto_start_event` (`corrective_action.submitted`, `corrective_action.created`, `finding.raised`) is started by that domain event *after the domain
transaction committed*; a refusal (no assignee, start condition, already running) is audited as `workflow.auto_start_skipped` and never breaks the domain flow.

## Transitions (`engine.perform`) - the only way an instance changes state
In one transaction under the instance row lock: (1) replay a repeated request (`idempotency_key`, durable); (2) refuse a non-active instance (409) or a stale `expected_revision` (409); (3) look up the enabled transitions for the action from
the **server-side** current state (an unknown action or a target-state name is `invalid_transition`); (4) select the branch: the first by order whose Phase 6 condition is TRUE - if none, `no_branch` (**fail closed**); (5) authorize
the actor by the transition's `actor_rule` - *assignee* (holds a task, or is eligible for the group task, which is claimed by acting), *starter* (or a holder of `workflow.transition`), *authorized* (`workflow.transition`) - plus the
state's **separation-of-duties** options and visibility of the record; (6) require a reason where configured; (7) multi-approver modes record the approval and wait; (8) run the registered **business action** through the domain service;
(9) move the instance (revision +1, new state visit), complete / cancel the visit's tasks, create the next state's tasks (or complete the instance on a terminal state); (10) append the event and the audit entries.
A refusal leaves everything unchanged and is audited (`workflow.transition_refused`, or `workflow.handler_failed` when the domain service refused).

## Business action registry (`handlers.py`, ADR 0071)
`corrective_action.issue|start|block|resume|submit|verify|reject|close|cancel|reopen` and `finding.acknowledge|start|resolve|close|reopen|cancel`. Each handler calls the module's lifecycle service
(`apps.corrective_actions.services.transition`, `apps.findings.services.transition`) as the acting user; the domain service rechecks authorization, separation of duties and the record's state. A designer can only choose a registered
code for the version's target type. **Extending**: add a `TargetSpec` (`targets.py`: authorized resolver, organization / location, owner / creator, `facts`, events) and `Handler`s, register both in `integration.py`; nothing else changes.
**Transactional strategy**: the workflow transition and the domain operation commit together or not at all; a domain refusal rolls the transition back, so the workflow never claims an operation that did not happen. There are no external
side effects inside the transaction in Phase 14; a future one must use after-commit signals or a reliable-processing pattern (an outbox) - not added now.

## Governance: no bypass, no drift
While an active instance's current state has an enabled transition that performs a domain action (e.g. `corrective_action.verify`), a **direct** call to that domain service is refused (`governed_by_workflow`) via a guard registered with
Phases 12/13; the engine's own call passes. Once the workflow finishes or is cancelled the domain module is free again.

## Tasks (`services/tasks.py`)
Assignee policies: **user** (one task per eligible named person), **permission** (holders of a registered permission at the location), **role** (holders of a role); group policies create **one claimable task** - the engine never picks an
arbitrary user. Eligible = active member + matching the policy + able to see the record + not violating separation. If nobody is eligible the state's policy applies: *block* (the transition fails, nothing is created) or *attention*
(`needs_attention` is set, an unassigned task is created, and an administrator reassigns). `claim` / `release` / `reassign` are authorized (claim needs `workflow.task_claim`, reassign `workflow.task_reassign` and an eligible new assignee) and audited;
a task is **never an authorization grant**. **Orphan policy** (`release_workflow_orphans`): a task whose assignee became inactive or ineligible returns to the pool (group slot) or is released and the workflow flagged (named task).

## Approvals and separation of duties
Modes: `one` (any eligible approver decides), `all` (every named approver must approve), `min` (N distinct approvers from the group). The same person can never count twice (database unique per actor and visit); a rejection fires at once; a group task
stays open for the other approvers until the minimum is reached. Separation options on a state: `not_starter`, `not_target_owner`, `not_target_creator`, `not_previous_actor`; they can only **add** restrictions - the domain service's own rules
(e.g. a corrective action's owner / submitter cannot verify) always apply too.

## Deadlines
A state's `due_hours` sets the task's `due_at` when the state is entered (timezone-aware). **Overdue is derived** (`task.is_overdue`, `selectors.overdue_tasks`) - an open task of an active workflow past its due date - never stored, no job required.

## Cancellation
`engine.cancel` (`workflow.cancel`, reason required) cancels the open tasks and marks the instance cancelled; **the business record is not touched**. Completed / cancelled instances free the record for a new workflow.

## Notification integration points (not implemented)
Notifications can subscribe to the activity-log verbs above, read `WorkflowEvent`, query `task.due_at` / `selectors.overdue_tasks` / `instance.needs_attention`, and use the domain signals of Phases 12-13. No delivery, reminder or escalation job exists.

## Testing and troubleshooting
```bash
python -m pytest tests/test_workflow_definitions.py tests/test_workflow_engine.py tests/test_workflow_api_web.py
```
| Symptom | Likely cause |
|---|---|
| "Nobody is eligible to handle ..." | the state's policy matches nobody who may see the record and is not excluded by separation; fix the policy or use *attention* |
| `governed_by_workflow` on a domain action | an active workflow governs that decision; decide it through the workflow task (or cancel the workflow) |
| `no_branch` | none of the conditional branches matches (or an attribute is unknown): add a fallback branch |
| A task disappeared from "available to claim" | someone claimed it, the instance finished, or you no longer match the policy / separation |
| Auto-start did nothing | see `workflow.auto_start_skipped` in the activity log (code says why) |
