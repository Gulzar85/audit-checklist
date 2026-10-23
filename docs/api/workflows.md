# Workflow API

Base `/api/v1/`. Envelope and authentication as the rest of v1. Scoped by organization (design) and location (runtime): invisible / foreign / malformed -> `404`, forbidden -> `403`, stale / repeated / not-active -> `409`, validation -> `400`.
There is no endpoint that sets a state, a task status, a target model or an actor. Unknown fields are rejected.

## Discovery
`GET /workflows/metadata/` - the controlled vocabularies: target types (with attributes and auto-start events), business actions (`handlers`), Phase 6 operators, separation options, state kinds, actor rules, decisions, approval modes.

## Definitions and versions
| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/workflows/` | list / create a definition `{organization?, code, name, target_type, description?, is_default?, auto_start_event?}` (creates an empty draft) |
| GET / PATCH | `/workflows/{id}/` | read / change name, description, default flag, auto-start event |
| GET / POST | `/workflows/{id}/versions/` | list versions / create a draft `{change_summary?, clone_from?}` (one draft at a time: `409 draft_exists`) |
| POST | `/workflows/{id}/archive/` | archive (refused while instances run) |
| GET / PATCH | `/workflow-versions/{id}/` | the full version (states, transitions) / change a **draft** (`change_summary`, `start_condition`, `allow_loops`) |
| POST | `/workflow-versions/{id}/validate/` | `{ok, errors[], warnings[]}` |
| POST | `/workflow-versions/{id}/publish/` | validate and publish; `400 validation_failed` with the report on errors |
| POST | `/workflow-versions/{id}/clone/` | a new draft from this version |
| POST | `/workflow-versions/{id}/archive/` | archive a superseded version |
| GET | `/workflow-versions/{id}/preview/` | states with outgoing transitions (conditions as text) + validation |
| GET / POST | `/workflow-versions/{id}/states/` | list / add a state (draft only) |
| PATCH / DELETE | `/workflow-versions/{id}/states/{code}/` | change / remove (draft only; a state in use cannot be removed) |
| GET / POST | `/workflow-versions/{id}/transitions/` | list / add a transition `{source, target, action, label, handler?, actor_rule?, decision?, reason_required?, condition?, sort_order?, enabled?}` |
| PATCH / DELETE | `/workflow-versions/{id}/transitions/{transition_id}/` | change / remove (draft only) |

Any write to a non-draft version: `409 version_immutable`. Assignee policy: `{"type": "user", "users": [ids]}`, `{"type": "permission", "permission": "<registered code>"}` or `{"type": "role", "role": "<slug>"}`.
Condition: `{"attribute", "operator", "value"}` or `{"logic": "AND"|"OR", "conditions": [...]}`.

## Instances
| Method | Path | Purpose |
|---|---|---|
| GET | `/workflow-instances/` | scoped list; filters `status`, `target_type`, `target_id`, `workflow` (code) |
| POST | `/workflow-instances/` | start `{target_type, target_id, workflow?}` (needs `workflow.start`; the record is resolved through the registry; `409 already_running`) |
| GET | `/workflow-instances/{id}/` | the instance with `available_transitions` for the caller and this step's tasks |
| GET | `/workflow-instances/{id}/available-transitions/` | `{revision, results: [{action, label, decision, reason_required, target_state, handler}]}` |
| POST | `/workflow-instances/{id}/transitions/` | `{action, comment?, idempotency_key?, expected_revision?}` (also the `Idempotency-Key` header); response includes `replayed`, `pending_approvals`, `handler_outcome` |
| GET | `/workflow-instances/{id}/history/` | the event stream, newest first |
| POST | `/workflow-instances/{id}/cancel/` | `{reason}` - the business record is not changed |

## Tasks
`GET /workflow-tasks/` (scoped; `status`, `instance`, `overdue=1`), `GET /workflow-tasks/{id}/`, `POST .../claim/`, `.../release/`, `.../reassign/` (`{assignee}`), `GET /workflow-tasks/claimable/`, `GET /workflow-tasks/summary/`, `GET /workflow-tasks/mine/`, `GET /my-workflow-tasks/`.

## Error codes (`error.details.code`)
`invalid_transition`, `no_branch`, `reason_required`, `stale_revision`, `already_approved`, `not_active`, `already_running`, `handler_failed`, `no_assignee`, `no_workflow`, `start_condition_not_met`, `target_not_eligible`, `invalid_target_type`, `validation_failed`,
`version_immutable`, `draft_exists`, `invalid_condition`, `invalid_handler`, `invalid_assignee_policy`, `invalid_separation`, `already_claimed`, `invalid_assignee`, `separation_of_duties`.
