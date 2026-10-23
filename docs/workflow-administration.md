# Workflow administration and configuration

## Permissions (`workflow.*`)
Design (organization-wide): `view`, `create`, `update_draft`, `validate`, `publish`, `archive`. Runtime (location-scoped): `start`, `transition`, `cancel`, `instance_view`, `task_view`, `task_claim`, `task_reassign`.
(There is no separate `approve` permission: approving is a task decision, authorized by the assignee policy and by the business action's own permission; history is visible with `instance_view`.) Administrator and QA Manager hold all; Auditor, Operations
Manager and Restaurant Manager hold the runtime permissions that fit their work. After deployment run `manage.py seed_permissions` and `seed_roles`. System workflows are created and published only by platform superusers.

## Designing a workflow (web: *Workflows*; API: [api/workflows.md](api/workflows.md))
1. **New workflow**: code (permanent), name, the record type it applies to, whether it is the default, and an optional automatic start event. An empty draft is created.
2. **States**: code, name, kind (one *initial*, at least one *success* end, optional *unsuccessful* end), and for approval stages the **assignee policy** (named users, holders of a permission, holders of a role), approval mode, separation options, task deadline and what to do when nobody is eligible.
3. **Transitions**: from / to state, action code and label, who may do it, whether it counts as an approval / rejection, whether a reason is required, an optional **business action** (from the registry) and an optional **condition** (attribute / operator / value, or an advanced AND/OR JSON tree). Branches: several transitions with the same action from one state are tried in order; keep an unconditional fallback last.
4. **Validate** (errors block publication, warnings advise), read the **workflow map**, then **Publish**. Edit later by cloning into a new draft; running workflows keep their version.

## Commands
| Command | Purpose |
|---|---|
| `seed_workflows --organization <slug>` | creates and publishes *Corrective action verification* (default for corrective actions, starts when an action is submitted) and *Critical finding review* (conditional approval) |
| `release_workflow_orphans [--organization <slug>]` | release tasks whose assignee is inactive or ineligible (safe to schedule; correctness does not depend on it) |

## Admin
Read-only for definitions, versions, states, transitions, instances, tasks and events (no add / change / delete / bulk action); non-superusers see only their scope. Published configuration is also frozen by database triggers; events are append-only.

## Enabling the corrective action verification workflow
`seed_workflows`, then submit an action: the workflow starts, holders of `corrective_action.verify` (other than the owner and submitter) find it under *Workflow tasks*, and **approve / reject there** - direct verification in the corrective action
screen is then refused while the workflow runs. To go back to direct verification, archive the workflow (cancel running ones first).
