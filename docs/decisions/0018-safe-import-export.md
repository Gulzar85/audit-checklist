# 0018. Imports are all-or-nothing, code-keyed and tenant-safe

- **Status:** Accepted
- **Date:** 2026-10-15

## Context

A location import is a bulk write to a *tree*. A half-applied file, a parent that
appears after its child, a cycle, or a row aimed at another tenant must not corrupt
the hierarchy or leak across organizations.

## Decision

(`apps/organizations/resources.py`, `apps/locations/resources.py`, django-import-export)

- **One transaction per import**; the admin rolls back on any validation error, so a
  bad file never partially applies. A dry run is always available.
- Rows are keyed by **business codes** (`organization`, `location_type`, `parent`) and
  resolved *within the row's organization*. A parent/type that exists only in another
  organization is simply not found: a cross-organization link cannot be expressed.
- Rows are **re-ordered parents-first**, so files may list children before parents;
  duplicate `(organization, code)` rows and circular parent chains *within the file*
  are rejected up front.
- Every row is validated with `full_clean` (duplicate codes, bad status, bad
  country/timezone, inactive type/parent, ...) and **authorised through the policy
  layer** for the importing user (`user=` is required).
- Foreign and non-existent organizations yield the *same* error class and message.
- Lifecycle (`status` / `is_active`) may be **set on new rows only**; changing it on an
  existing row is rejected - transitions have audited operations of their own.
- Created/updated rows are logged with `source=import` and keep model history.

## Consequences

- `tests/test_import_export.py` covers atomicity, ordering, cycles, cross-tenant rows
  and the indistinguishable-error rule.
- Import is an admin (staff) feature in Phase 1; a self-service import UI could reuse
  the same resources unchanged.
