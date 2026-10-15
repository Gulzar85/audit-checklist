# 0014. Deactivate, never delete; no generic soft-delete column

- **Status:** Accepted
- **Date:** 2026-10-15

## Context

Locations and organizations will be referenced by checklist templates, scheduled
assignments, executions, findings, reports and audit history. Deleting them - or
cascading a delete - would destroy business history. A blanket `deleted_at` on
every model is a heavy convention (every query must remember to filter it).

## Decision

- **No `deleted_at`.** `Organization.status` and `Location.is_active` /
  `LocationType.is_active` *are* the lifecycle. "Retired" is a first-class state
  that history, reports and later phases can still reference.
- **No delete in the UI or API** (no DELETE endpoints/buttons). The admin disables
  deletion for organizations and locations.
- All relevant foreign keys are **`PROTECT`**: `Location.organization`,
  `Location.parent`, `Location.location_type`, `LocationType.organization`. Later
  phases reference locations the same way. A cascade can never silently remove history.
- **When a location is deactivated** (`services.deactivate_location`):
  - the location, its history and everything linked to it are kept;
  - an *active* location can never sit beneath an *inactive* parent, so a location
    with active descendants is refused unless `cascade=True` is given, in which case
    the whole active subtree is deactivated in one transaction (each node keeps its
    own history row; one activity-log entry records the event);
  - a location can only be **re**activated once its parent is active, so inactive
    subtrees are always whole and "active locations" never needs an ancestor walk;
  - new locations cannot be created or moved beneath an inactive parent.
- **Location types**: deactivating one leaves existing locations untouched; the type
  simply cannot be newly assigned.
- **Organizations**: see ADR 0011.

## Consequences

- Future phases may rely on `PROTECT`: a location with checklist data cannot be
  removed even by the admin.
- Mistaken records are deactivated; a typo'd, never-used location can still be
  corrected by editing it.
- If hard deletion is ever required (e.g. data-subject requests) it must be a
  deliberate, separately designed operation.
