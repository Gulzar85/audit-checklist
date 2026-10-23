# Workflow engine architecture (Phase 14)

The workflow engine **coordinates** configurable business processes. It does not own the domain rules of findings or corrective actions: every business operation a
transition performs is a call to that module's own lifecycle service, with the acting user.

## Domain model (`apps/workflows`)

| Model | Purpose | Integrity |
|---|---|---|
| `WorkflowDefinition` | the stable identity: code, name, `target_type`, default flag, `auto_start_event`; **system** (no organization) or organization-specific | unique code per organization (and among system workflows); one *default* per organization + target type (and one system default per target type) |
| `WorkflowVersion` | independently versioned configuration: `draft -> published -> superseded / archived`, `start_condition`, `allow_loops`, `config_hash` | unique number per definition; **one draft and one published version per definition**; a published version is sealed by a database trigger |
| `WorkflowState` | a configured state: `kind` (initial / intermediate / success / failure), **assignee policy**, approval mode, separation options, task deadline, no-assignee policy | unique code per version; exactly one initial state (partial unique index) |
| `WorkflowTransition` | an allowed move: action code, source, target, business action (`handler`), actor rule, decision kind, reason policy, **Phase 6 condition**, branch order | unique branch per (source, action, order); connects states of its own version (trigger) |
| `WorkflowInstance` | a workflow running on one record: the **exact version**, registered `target_type` + `target_id`, location, current state, status/outcome, `entry_seq`, `revision` | one *active* instance per (target, definition) (partial unique); organization/definition/version/target/location immutable and consistent (trigger); never deleted |
| `WorkflowTask` | work for one named user or one claimable group; status pending / claimed / completed / cancelled | unique per (instance, state visit, slot) - a repeated event cannot duplicate a task |
| `WorkflowEvent` | the append-only decision / transition history; also the durable idempotency record and approval ledger | unique `(instance, seq)`, `(instance, idempotency_key)` and one approval per actor and state visit; update/delete forbidden (trigger) |

The activity log receives the same events (`workflow.started`, `.transition_completed`, `.transition_refused`, `.handler_invoked`, `.handler_failed`, `.approval_recorded`,
`.task_created|claimed|released|reassigned`, `.completed`, `.cancelled`, `.needs_attention`, plus design events) so platform auditing stays in one place; `WorkflowEvent` exists
because the engine itself must reason from durable, ordered, immutable facts (idempotency, multi-approver counting, replay).

## States, steps and transitions (ADR 0069)
There is **no separate "step" entity**. An approval stage is a *state* with an assignee policy; the decisions of that stage are the *transitions* leaving it. A state with a policy creates tasks
when entered; its transitions say who may take which decision (`actor_rule`), whether a reason is required, which branch applies (`condition`) and which business action runs (`handler`).
This avoids three overlapping concepts (state / transition / step) that would have to be kept consistent.

## Instances, tasks and one state per visit
`entry_seq` counts visits to states: tasks, approvals and idempotency are scoped to a visit, so a loop never reuses stale decisions. `revision` is the optimistic-concurrency token of the instance.
Several workflows (different definitions) may run side by side on one record; two active instances of the same definition on one record are refused (database constraint).

## Where to read next
[workflow-versioning-and-validation.md](workflow-versioning-and-validation.md) · [workflow-runtime.md](workflow-runtime.md) · [workflow-administration.md](workflow-administration.md) · [api/workflows.md](api/workflows.md) · ADRs 0069-0072.

## Known limitations
* No notifications, reminders or recurring escalation jobs: the data exists (`due_at`, derived `is_overdue`, `needs_attention`, the event stream) and after-commit domain signals remain the integration points.
* Evidence requirements are not a transition option; the business actions' own services enforce evidence (e.g. completion files before a corrective action can be submitted).
* Moving a running instance to a newer version is not supported (cancel and restart); no visual (drag-and-drop) designer - a structured editor with a readable map is provided.
* A domain change made *outside* the workflow while no workflow governs it does not move the instance; while a workflow governs a decision, the domain refuses the direct call (see [workflow-runtime.md](workflow-runtime.md)).
* Branch conditions can only compare attributes registered for the target type (listed by `GET /api/v1/workflows/metadata/`).
