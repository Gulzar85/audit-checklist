# Assignment security and scope

Assignments follow the layered model of [authorization.md](authorization.md): organization isolation, then RBAC permission,
then **location scope**. One decision service (`apps.permissions.authorization`) is used by the UI, the API, the admin, bulk
operations and the services - there is no second rulebook.

## Permissions

| Code | Allows | Default roles |
|---|---|---|
| `assignment.view` | see assignments in scope | administrator, QA manager, operations manager, auditor, restaurant manager, restaurant user |
| `assignment.create` | create drafts at a location | administrator, QA manager, operations manager |
| `assignment.update` | edit drafts; change due date / priority / responsible user | administrator, QA manager, operations manager |
| `assignment.issue` | issue drafts | administrator, QA manager, operations manager |
| `assignment.cancel` | cancel | administrator, QA manager, operations manager |
| `assignment.reopen` | reopen a cancelled assignment | administrator, QA manager |
| `assignment.bulk_create` | bulk assignment | administrator, QA manager |

Roles are data: the matrix is seeded and editable per organization. Viewing a checklist definition (`checklist.view`, an
organization-wide permission) is required to *choose* a version.

## Location scope

A grant may be organization-wide, on one location, or on a location plus its descendants. The permission is checked **at the
assignment's location** (`has_permission(user, code, location=...)`), and querysets are built by
`selectors.assignments_visible_to(user)`: org-wide holders see their organizations; scoped holders see assignments whose location
is in their granted subtrees. A user designated as the **responsible user** of an *issued* assignment can see that one assignment
(never drafts) without any role, but cannot change or cancel it.

## 404 vs. 403

* An assignment, location, version or user the caller cannot see - because it belongs to another organization or lies outside
  their scope - is **404** (web and API). Its existence is never confirmed. A forged filter value or UUID reveals nothing.
* An assignment the caller *can* see but may not change is **403**.
* Creating with a location/version the caller does not know about is a validation error ("Unknown location/version", 400) in the
  API and "Select a valid choice" in forms - the same generic answer for "does not exist" and "not yours". In bulk operations it is
  the same generic "not available" entry.

## Other controls

* **Mutations are POST-only**, protected by CSRF; the confirm dialogs are not a security control - the services re-authorize.
* **No mass assignment**: the API rejects `status`, `organization`, `created_by`, `number`, `version`, `location` and friends with
  400; PATCH accepts only priority, due date, instructions (drafts) and responsible user.
* **Responsible user eligibility**: must be an active user, an active member of the organization, and allowed to run checklists
  at that location; the options list is built from the same rule and limited to the caller's scope.
* **Cross-organization integrity** is enforced three times: services, the service-side location/version fetch, and the
  PostgreSQL trigger.
* **Output encoding**: instructions and reasons are escaped by the templates; nothing is rendered as HTML.
* **Audit**: every successful change and every refused transition is logged with the actor.
* **Admin**: read-only (no add/change/delete, no actions) and restricted to the same scope for non-superusers.

