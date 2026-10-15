# 0012. Authorization decisions live in policies, shared by every channel

- **Status:** Accepted
- **Date:** 2026-10-15

## Context

The same rule - "may this user edit this location?" - applies to HTML pages, the
REST API, imports and (later) background work. Re-implementing it per channel
guarantees drift; branching on role names (`if user.role == "Administrator"`)
couples the platform to one org chart (ADR 0007).

## Decision

- Each domain has a `policies.py` of plain functions answering
  *"may `user` do X to `obj`?"*: `can_view_organization`, `can_manage_organization`,
  `can_change_organization_status`, `can_create_location`, `can_edit_location`,
  `can_move_location(user, location, new_parent)`, `can_edit_location_type`, ...
- A rule combines three data-driven facts - **membership/visibility**, **Django
  model permission**, **platform-staff status** - never a role name. Role/scope
  refinements (Phase 4) change the *bodies* of these functions, not their callers.
- Channels adapt, they do not decide: `apps.core.mixins.PolicyMixin` for CBVs,
  `apps.api.permissions.PolicyPermission` for DRF, `validate_instance` for imports.
- **404 vs 403.** Objects outside the user's scope are filtered out by the selector
  first, so they are **404** (existence not revealed). Visible-but-forbidden is **403**.
- Platform-wide location types are managed by platform staff only (ADR 0016).

## Consequences

- One definition per rule; `tests/test_policies.py` is a single matrix over every
  kind of user, and `test_no_policy_inspects_role_names` guards the convention.
- Services contain business rules, *not* authorization: entry points authorise,
  then call a service. A new entry point must call the policy (and the isolation
  tests in `tests/test_tenant_isolation.py` exist to catch a forgotten one).
- Django model permissions are global to a user; per-organization/per-location
  grants arrive with the Phase 4 scope engine, which these signatures already accommodate.
