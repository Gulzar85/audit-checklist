# 0016. Location types may be platform-defined (nullable organization)

- **Status:** Accepted
- **Date:** 2026-10-15

## Context

Every organization defines its own location types, but many want the same
generic vocabulary (`Site`, `Region`). Forcing each to re-create it is tedious;
making types global-only would defeat per-organization hierarchies.

## Decision

- `LocationType.organization` is **nullable**: `NULL` means a *platform-defined*
  type usable by every organization (this mirrors how roles work, ADR 0007).
- Uniqueness is two partial constraints: `(organization, code)` where the
  organization is set, and `(code)` where it is not.
- Only **platform staff** may create, edit or toggle platform-defined types
  (`policies.can_*_location_type`). Organization admins can *use* them but not
  change them, otherwise one tenant could alter another's vocabulary.
- A type cannot be moved to another organization (service rule), and the database
  refuses to re-home a type that other organizations' locations already use.
- Choosing a type: `available_location_types(org)` = the organization's active types
  plus active platform types.

## Consequences

- No seed data is required: the demo command creates organization-scoped types only.
- A platform type cannot be deleted while any location uses it (`PROTECT`).
