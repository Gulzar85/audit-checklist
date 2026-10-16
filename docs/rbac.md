# Roles, permissions and scope (RBAC)

See ADRs 0042-0044. Model: `Permission` (registry mirror) -> `RolePermission` -> `Role` -> `MembershipRole`
(membership + scope: `organization`, `location`, `location_descendants`).

## Registry
`apps/permissions/registry.py` lists every permission (`resource.action`), its legacy Django permission and the
system roles: Super Administrator, Administrator, QA Manager, Auditor, Operations Consultant, Operations Manager,
Restaurant Manager, Restaurant User, Report Viewer. Run `python manage.py seed_permissions` and
`python manage.py seed_roles` (both idempotent; `seed_roles` also syncs permissions).

## Membership status
`pending`, `active`, `suspended`, `left` (= the spec's INACTIVE). Only `active` grants access; roles are kept.

## Screens and API
`/access/roles/`, `/access/roles/<id>/` (permission matrix), `/access/members/`, `/access/members/<id>/` (roles,
scope, status), `/access/me/`. API: `/api/v1/roles/`, `/permissions/`, `/members/`, `/members/{id}/roles/assign/`,
`/access/me/`; pass `?organization=<id>` (validated; forged = 404).

## Audit
ActivityLog verbs: `role.created|updated|deactivated|activated|permissions_changed`,
`membership_role.assigned|removed|scope_changed`, `organization_membership.*`; simple_history on roles and assignments.

## Settings
`RBAC_CACHE_TTL`, `RBAC_LEGACY_MODEL_PERMISSIONS` (default on), `CACHES`.

## Upgrading existing data
Members created before Phase 3 hold no role and therefore see no locations. Run `seed_roles`, then assign roles
(for example Administrator) from `/access/members/` or the admin. Superusers are unaffected.
