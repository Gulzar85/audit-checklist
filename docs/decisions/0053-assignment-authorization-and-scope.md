# 0053. Assignment authorization: scope in the selector, 404 for invisible, 403 for forbidden

- **Status:** Accepted
- **Date:** 2026-10-16

## Context
Assignments are location-scoped operational records. The same rules must hold in the UI, the REST API, bulk operations and the
admin, and an unauthorized caller must learn nothing about other organizations' or areas' data.

## Decision
* Seven permissions (`assignment.view/create/issue/update/cancel/reopen/bulk_create`) are checked **at the assignment's
  location** through the single `has_permission` service (ADR 0042-0044).
* Reads go through `selectors.assignments_visible_to` (organization isolation + scope, plus the designated responsible user's
  own *issued* assignment). Anything outside it is **404**; visible-but-forbidden is **403** (ADR 0043). Forged filter values and
  UUIDs are validated against the same authorized sets.
* Writes always go through services, which re-authorize; views and the API are adapters and the admin is read-only.
* Lifecycle actions check **scope before the transition rule**, so an out-of-scope caller cannot probe state through different
  error messages.
* Lookups of related objects at creation (location, version, user) answer unknown and unauthorized identically.

## Consequences
* One place to audit, identical behavior across channels, and a cross-organization test matrix that is channel-independent.
* The responsible-user exception grants read access only; it never confers any write permission.
