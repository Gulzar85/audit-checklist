# Identity architecture

Keep five concepts separate. Phase 2 implements the first three; the last two plug in later without
redesigning the user or membership tables.

| Concept | Question | Where it lives |
| --- | --- | --- |
| Authentication | Who are you? | Microsoft Entra ID (MSAL), verified server-side |
| Identity | Which application user are you? | `accounts.User` (`entra_object_id`, `status`) |
| Organization membership | Which organizations may you access? | `organizations.OrganizationMembership` (`status`) |
| Authorization | What may you do? | later RBAC phase, behind `apps/*/policies.py` |
| Scope | Which part of the organization? | later scope phase; data foundation: `MembershipLocationScope` |

```
Microsoft Entra ID -> External identity (object id + tenant) -> Django User -> Membership(s)
   -> current organization (session, validated per request) -> [RBAC] -> [location scope]
```

## The user
One custom `User` (Phase 0), never replaced. `email` is a login handle, **not** the identity key; the
permanent external key is `entra_object_id` (unique, nullable), with `entra_tenant_id` recorded beside
it. A tenant is a *value*, not a table: a tenant row would add nothing until per-tenant configuration
exists. Allowed tenants come from `MICROSOFT_ALLOWED_TENANTS` (default: the configured tenant).
`status` (`pending|active|suspended|inactive`) is authoritative; `is_active` is its stored mirror,
enforced by a check constraint (ADR 0030).

## Membership
A user belongs to organizations through `OrganizationMembership`, never a column on `User`.
`status` (`active|suspended|left`) is authoritative with an `is_active` mirror and `left_at`; rows are
kept when someone leaves (ADR 0035). A membership says only "this user may operate in this
organization". Roles and permissions are not on it.

## Preferences
Appearance (light/dark/system) belongs to the theme engine (`themes.UserAppearance`, ADR 0034). Other
user choices live in `UserPreference`, an allow-listed key/value store (`timezone`, `language`) with a
validator per key (ADR 0037). No column per preference.

## Location scope foundation
`MembershipLocationScope(membership, location, include_descendants)` records which location roots a
membership is intended to cover. It is **data only**: nothing reads it to restrict access yet
(ADR 0036).

## Layers
Selectors read (`selectors.py`), policies decide (`policies.py`), services write (`services.py`:
provision, link, unlink, sync, lifecycle, membership, preferences, audit), adapters adapt (views, API,
admin, backend, middleware). No view hard-codes a role.

## Services at a glance
`resolve_entra_user`, `provision_user`, `link_external_identity`, `unlink_external_identity`,
`sync_user_identity`, `set_user_status` (+activate/suspend/deactivate), `add_membership`,
`set_membership_status`, `remove_membership`, `set_default_organization`, `set_active_organization`,
`set_preference`, `record_authentication`.

## Audit
All through `ActivityLog`: `user.created|updated|activated|suspended|deactivated`,
`identity.linked|unlinked`, `organization_membership.created|updated|suspended|removed`,
`membership_scope.added|removed`, `user.preference_changed`, `authentication.success|failure`,
`logout`, `organization.switched`. History (simple-history) records `User` and membership changes.
Never logged: passwords, tokens, secrets, raw claims.