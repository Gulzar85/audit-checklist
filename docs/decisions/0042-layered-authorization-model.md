# 0042. Layered authorization: membership, role, scope

- **Status:** Accepted
- **Date:** 2026-10-16
- **Extends:** [0012](0012-policy-layer.md), [0035](0035-membership-status-lifecycle.md), [0036](0036-location-scope-foundation.md)

## Decision
Authorization is four layers, each answering one question: **authentication** (who), **organization membership**
(may they enter this tenant), **role** (what), **scope** (where: whole organization, one location, or a location
and its subtree). `apps/permissions/authorization.py` is the single decision service; views, the API, forms,
selectors and templates all call it.

* Permissions are `<resource>.<action>` codes in a code registry (`apps/permissions/registry.py`), mirrored to the
  `Permission` table by `seed_permissions`. Code uses `P.LOCATION_UPDATE`; an unknown code raises.
* Roles are data. System roles (organization = null, `is_system`) are seeded and immutable; organizations create
  custom roles. `MembershipRole` attaches a role to an **organization membership** (not to a user) with a scope.
* Grants are **additive**; there are no deny rules. **Default deny**: no active membership, active organization,
  active role or registered code means no access.
* Only an **active** membership grants anything. `pending`, `suspended` and `left` (the spec's *inactive*) keep
  their roles, so reinstating restores access exactly.
* `is_superuser` bypasses, in one documented place. `is_staff` is *not* a bypass.
* Legacy Django model permissions remain an organization-wide grant while `RBAC_LEGACY_MODEL_PERMISSIONS` is on.

## Consequences
A member with no role sees no locations (previously any member could read). Existing members need a role
(`seed_roles`, then assign). Moving a location changes subtree access immediately because subtrees are expanded
per request, never cached.
