# 0011. One authoritative lifecycle field for organizations

- **Status:** Accepted
- **Date:** 2026-10-15

## Context

An organization can be in service, retired by its owner, or suspended by the
platform operator. A boolean `is_active` plus a `status` column would be two
sources of truth that can disagree.

## Decision

- A single **`status`** field: `active` | `inactive` | `suspended`, guarded by a
  database `CHECK`. There is **no** `is_active` column.
- `Organization.is_active` is a derived property (`status == active`), and the
  `is_active` filter/serializer field are computed from it.
- Transitions happen only through `services.set_organization_status()` (or its
  `activate_/deactivate_/suspend_` wrappers), which lock the row, log the event and
  record history. `status` is read-only in forms and the API, and an import may
  *set* it on a new organization but never *change* it on an existing one.
- Only **platform staff** may change status (see ADR 0012): if members could
  deactivate their own organization they would lose sight of it and could not undo it.
- **Effect.** An inactive/suspended organization is invisible to its members
  (`organizations_visible_to`), so every UI page and API call for it is a 404, and
  every location/type write refuses (`require_active_organization`) - even for
  platform staff - until it is reactivated.
- **Nothing is deleted.** Locations, types, history and logs remain.

## Consequences

- "Inactive organizations cannot operate" is enforced once, at the visibility
  boundary and again at the write boundary, for the UI, API, admin actions and imports.
- Locations and location types keep the simpler boolean `is_active`: they have
  exactly two states, so there is still only one authority per model.
