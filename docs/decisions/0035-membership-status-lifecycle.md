# 0035. Membership lifecycle: one authoritative status, rows are kept

- **Status:** Accepted
- **Date:** 2026-10-15
- **Builds on:** [0030](0030-user-lifecycle-status-authoritative.md), [0015](0015-tenant-isolation-through-selectors.md)

## Context
`OrganizationMembership` had an `is_active` boolean, and "removing" a member hard-deleted the row,
losing history and any later references (assignments, checklist data).

## Decision
`status` (`active | suspended | left`) is authoritative, with `left_at`. `is_active` stays as a **stored
mirror** (`status == active`) so every tenant filter and selector keeps working unchanged. Consistency is
enforced three ways: `save()` derives the mirror and `left_at`; check constraints forbid contradictory
rows; services always move status and mirror together. A legacy write that only sets `is_active=False` is
read as *suspended*. Leaving keeps the row (`left`, `left_at`, `is_default` cleared); re-joining reuses it.
All paths (web, API, admin inline and actions) call `set_membership_status`, which audits
`organization_membership.created|updated|suspended|removed` and writes history. Membership rows are no
longer deletable from the admin.

## Consequences
* Isolation is unchanged: only `active` memberships grant access.
* A data migration maps existing `is_active=false` rows to `suspended`.
* Direct `QuerySet.update(is_active=...)` now violates a constraint - intended; use the service.