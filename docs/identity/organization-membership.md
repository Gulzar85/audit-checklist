# Organization membership and context

## Membership
`OrganizationMembership(user, organization, status, is_default, joined_at, left_at)`; unique per
(user, organization).

| status | meaning | access |
| --- | --- | --- |
| `active` | may operate in the organization | yes |
| `suspended` | paused by an administrator | no |
| `left` | no longer a member (row kept, `left_at` set) | no |

`is_active` mirrors `status == active` (check constraint + `save()`), so every tenant filter keeps using
`is_active`. Re-joining reuses the row. All changes go through `services` (web, API, admin inline and
actions) and are audited (`organization_membership.created|updated|suspended|removed`).
`is_default` marks the preferred organization (cleared when the member leaves).

## Current organization
Not stored on the user. Per request, lazily (`apps/accounts/context.py`): the session's
`active_organization_id` is validated against `organizations_visible_to(user)`; if it is missing or no
longer valid, fall back to the default membership, then the first visible organization. The API does not
pay for this: it scopes through `organizations_visible_to` directly.

## Switching
`POST /organizations/switch/` (CSRF): the organization must be in the user's visible set (otherwise 404 -
indistinguishable from a non-existent id), the policy `can_switch_organization` must allow it, `next` is
validated, the session value is replaced and `organization.switched` is logged. A client-supplied
`organization_id` in a query string or form is never trusted on its own.

## Multi-organization users
One `User`, many memberships; the themed look follows the default membership's organization
(see `docs/themes/theme-resolution.md`).

## Location scope foundation
`MembershipLocationScope` (in `apps/locations`) links an *active* membership to a location of the *same*
organization (service, `clean()` and a PostgreSQL trigger agree), optionally including descendants.
Helpers: `scope.add_membership_scope`, `remove_membership_scope`, `scope_roots`, `scoped_location_ids`.
**Nothing enforces it**: a member with scope rows is not restricted in Phase 2 (ADR 0036).

## What this phase does not do
Roles, permissions, scope enforcement, assignments, notifications.