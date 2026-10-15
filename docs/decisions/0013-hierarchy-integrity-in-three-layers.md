# 0013. Hierarchy integrity is enforced in three layers, including PostgreSQL

- **Status:** Accepted
- **Date:** 2026-10-15

## Context

The rules - no self-parent, no cycles, no cross-organization parent, no
cross-organization location type, a location never changes organization - span
rows, so a plain `CHECK` cannot express most of them. Validation that exists only
in forms or services is bypassed by `QuerySet.update()`, bulk writes, a restore,
hand-written SQL, and races between two administrators.

## Decision

1. **Model** - `Location.clean()` / `LocationType.clean()` give field-level errors
   to forms, the admin, imports and the API.
2. **Service layer** - transactions, activity log, history, and an
   **organization row lock** (`SELECT ... FOR NO KEY UPDATE`) taken by every
   tree-changing operation, so concurrent administrators are serialised and each
   validation sees the other's committed result. Database errors raised by the
   layer below are translated to field errors (`as_validation_error`).
3. **PostgreSQL** (migration `locations.0004_integrity_triggers`) - `CHECK`
   constraints (self-parent, code/country format, coordinate pairs) plus
   `BEFORE INSERT/UPDATE` triggers enforcing same-organization parent/type,
   organization immutability and cycle prevention. The cycle check takes a
   per-organization **advisory lock** first, so `A under B` and `B under A`
   issued concurrently cannot both pass. A second trigger stops a location type
   being re-homed while other organizations' locations use it.

The triggers raise `check_violation` tagged with a constraint name; the service
layer maps each name to a user-facing message.

## Consequences

- `tests/test_hierarchy_integrity.py` proves every rule at all three levels,
  including a real two-thread race.
- Known limit: one SQL statement that rewrites *several* parents at once is
  validated row-by-row against the pre-statement snapshot. The
  `validate_location_tree` command (and `find_integrity_problems`) exists to detect
  such leftovers - and anything that arrives with triggers disabled.
- The "active location cannot sit beneath an inactive parent" rule (ADR 0014) is
  enforced in the model/services, not in a trigger; the audit command also reports it.
