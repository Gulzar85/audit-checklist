# 0036. Location scope: a data foundation without an engine

- **Status:** Accepted
- **Date:** 2026-10-15

## Context
Later phases will restrict a member to part of an organization's location tree. The user and
membership tables must be ready without redesign, but the scope engine (RBAC-aware enforcement) is out
of Phase 2.

## Decision
`MembershipLocationScope(membership, location, include_descendants)` lives in `apps/locations`, unique per
(membership, location), with history. The location must belong to the membership's organization - checked
by the service, `clean()` and a PostgreSQL trigger (FOR SHARE on the location row). Only active
memberships can be scoped. `scope.py` offers add/remove (audited as `membership_scope.added|removed`) and
read helpers (`scope_roots`, `scoped_location_ids`, which reuses the Phase 1 recursive-CTE descendant
query). **No selector, policy or API consults these rows**; a member with scope rows is not restricted. A
test pins that boundary.

## Consequences
The scope phase can attach enforcement (selectors/policies) to existing data. Roles are deliberately not
involved.