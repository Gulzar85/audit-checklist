# Tenant isolation and permissions

Decisions: [0012](../decisions/0012-policies-for-authorization.md) ·
[0015](../decisions/0015-tenant-isolation-through-selectors.md) ·
[0018](../decisions/0018-safe-import-export.md)

## The model

```
request ─► selector (scope) ─► 404 if outside the user's tenants
        └► policy (permit)  ─► 403 if visible but not allowed
        └► service (act)    ─► validated, locked, logged
```

Three questions, answered in three places - never mixed:

| Question | Where | Failure |
| -------- | ----- | ------- |
| *Can this user even see it?* | selectors (`organizations_visible_to`, `locations_visible_to`, ...) | **404** |
| *May they do this to it?* | policies (`apps/*/policies.py`) | **403** |
| *Is the change valid?* | services + model + database | **400** / field errors |

## Visibility

* anonymous or inactive user → nothing;
* platform staff (`is_staff`) → every organization (they operate the platform, not a tenant);
* everyone else → organizations where they have an **active membership** *and* the
  organization is **active**. Members of an inactive or suspended organization see
  nothing in it, on any channel.

## Permission rules (Phase 1)

| Action | Who |
| ------ | --- |
| View an organization / its locations and types | visible (above) |
| Create an organization | platform staff holding `organizations.add_organization` |
| Edit an organization, its branding | visible + `organizations.change_organization` |
| Activate / deactivate / suspend an organization | platform staff + `change_organization` |
| Create a location / type | visible organization + `add_location` / `add_locationtype` |
| Edit, activate, deactivate a location / type | visible + `change_location` / `change_locationtype` |
| Move a location | edit rights **and** visibility of the destination |
| Create / edit **platform-defined** types | platform staff + the model permission |
| Delete anything | nobody (deactivate instead) |

Django model permissions are global to a user; the data-driven **roles and scopes**
arrive in Phase 4 and refine who holds which permission *where* by changing the bodies of
the policy functions - their signatures already carry the organization, location and
destination a scope rule needs. Locations are a tree and `get_descendant_ids([...])`
already resolves several scope roots in one query, so "User A: Region 1's subtree; User B:
Region 2's" needs no model change.

## What is scoped (not just the main list)

* **Querysets** of every view and API viewset;
* **filter** relation fields (`organization`, `location_type`, `parent`) - choices and accepted values;
* **form / serializer** relation fields - a foreign id and a non-existent id give the same error;
* the **picker** (search results and the label it shows for a submitted id);
* **imports** (resolved within the row's organization; one neutral error);
* **admin** (staff only; platform staff are cross-tenant by definition; forms limit parent/type
  choices to the organization, and the model/triggers reject anything else).

## Uploads, CSP and CSRF

* Branding uploads: allow-listed extension, real-image check, size and pixel caps, generated names.
* Strict CSP stays intact: no inline scripts/styles/handlers; HTMX configured without eval or
  injected styles; Alpine only through registered components (ADR 0019).
* All state-changing views require POST + CSRF; status/deactivate actions are POST-only.
* Error responses never reveal other tenants: 404 for out-of-scope objects, identical field
  errors for foreign vs missing ids.

## Audit trail

Who / what / when / which organization / which object / what changed is answered by two
complementary records:

* `ActivityLog` (business events, JSON `changes: {field: {from, to}}`, `source` = admin/import);
* `simple_history` on `Organization`, `LocationType`, `Location` (field-level versions with the
  acting user and a change reason such as `location.parent_changed`).

## Tests that prove it

`tests/test_tenant_isolation.py` (every object URL/action, both directions, UI and API; data-leak
sweeps; suspended-organization behaviour), `tests/test_policies.py` (matrix over every user kind),
`tests/test_api_locations.py::TestIsolation`, `tests/test_import_export.py::TestImportTenantIsolation`,
`tests/test_hierarchy_integrity.py` (database-level rules).
