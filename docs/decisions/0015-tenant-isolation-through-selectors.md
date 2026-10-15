# 0015. Tenant isolation is enforced at one choke point: scoped selectors

- **Status:** Accepted
- **Date:** 2026-10-15

## Context

A user in Organization A must never read or change Organization B's data. Scattering
`filter(organization=...)` through views is how tenants leak: one forgotten filter is
a breach, and "hiding it from a dropdown" is not security.

## Decision

- `organizations_visible_to(user)` is **the** definition of visibility: nothing for
  anonymous/inactive users; everything for platform staff; for everyone else only
  *active* organizations they hold an *active* membership in.
- Tenant-owned selectors build on it: `locations_visible_to`,
  `location_types_visible_to` (own + platform-defined types), and so on. Views and
  viewsets take their `queryset` **only** from these, so an out-of-scope object is a 404.
- **Everything user-supplied is scoped too**, not just the main list:
  - django-filter relation filters (`organization`, `location_type`, `parent`) take
    their queryset from the requesting user's visible scope - a foreign id is an
    invalid choice, and the dropdown never lists other tenants;
  - form fields (`parent`, `location_type`, `organization`) and API
    `PrimaryKeyRelatedField`s use scoped querysets; a foreign id and a non-existent id
    produce the *same* error;
  - the location picker widget looks up its label **only inside its field's scoped
    queryset** (a `?parent=<foreign-id>` once echoed another tenant's location name);
  - imports resolve organization/type/parent by code with one neutral
    "not found, or not allowed" error for foreign and missing alike.
- `OrganizationScopedQuerySet` / `OrganizationScopedManager` (`apps/core/scoping.py`)
  provide `for_organization()`; future tenant-owned models reuse them.
- The database backs this up: organization immutability and same-organization
  parent/type triggers (ADR 0013).

## Consequences

- The web UI and the API share one security model; there is no "API mode".
- `tests/test_tenant_isolation.py` probes every object URL/action, both directions,
  and sweeps pages, fragments and API responses for the other tenant's data.
- Anything new that is tenant-owned must take its queryset from a selector built on
  `organizations_visible_to`; a view that queries a model directly is a review finding.
