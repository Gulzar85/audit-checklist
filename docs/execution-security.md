# Execution security

Layers (ADR 0042-0044, 0053): organization -> RBAC permission -> **location scope** -> assignment eligibility. One decision service
(`apps.permissions.authorization`) is used by the UI, the API, the admin and the services.

## Permissions

| Code | Allows | Default roles |
|---|---|---|
| `execution.view` | see executions in scope | administrator, QA manager, operations manager and consultant, auditor, restaurant manager and user |
| `execution.create` | start / resume | administrator, auditor, restaurant manager and user |
| `execution.update` | save answers | same |
| `execution.submit` | submit | same |
| `execution.cancel` | cancel an in-progress execution | administrator, QA manager |

Roles are data; the matrix is editable per organization. Viewing a checklist definition never grants the right to execute.

## Eligibility

Working on an assignment needs the permission **at the assignment's location** and the designation rule: an assignment designated
to someone is theirs alone; an undesignated one is open to the location's eligible users. A viewer can see an execution but not
change it; the workspace URL redirects them to the read-only detail and every write is refused by the services (403).

## 404 vs 403

* An execution or assignment the caller cannot see (other organization, outside scope) is **404**, in the web UI and the API, for
  reads and writes alike - its existence is not confirmed. Item ids from other versions or organizations and malformed ids all get
  the same generic `unknown_item`.
* Visible but forbidden is **403**; state conflicts (already submitted, stale revision, duplicate) are **409**.

## Other controls

* POST-only mutations with CSRF; the confirmation dialogs are not a security control.
* No mass assignment: the API rejects `status`, `organization`, `assignment`, `version`, `item`, submitter fields and unknown keys.
* The workspace form only touches items that were on the page, ignores media controls, and the server refuses hidden / foreign items.
* User text is escaped by the templates; comments and answers are never rendered as HTML.
* Everything is audited with the actor (`execution.started / responses_saved / submitted / submission_refused / cancelled`).
* Admin: read-only, no actions, scoped queryset.
* There are no exports or imports of executions.

