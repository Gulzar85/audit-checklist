# 0010. A generic location hierarchy: one tree, configurable types, no fixed depth

- **Status:** Accepted
- **Date:** 2026-10-15

## Context

Restaurants, warehouses, offices, hospitals and construction sites must all be
representable. One customer models `Country → Region → Area → Branch`; another
`Business Unit → Division → Facility → Department`. The platform must not know
either shape.

## Decision

- A single **`Location`** model with a self-referencing `parent` (an *adjacency
  list*), owned by an organization. There are no `Region`, `Area`, `Branch`
  models, and no maximum depth.
- A **`LocationType`** classifies each node (`Region`, `Facility`, ...). Each
  organization defines its own vocabulary; every location has exactly one type.
- Hierarchy reads use **PostgreSQL recursive CTEs** (`apps/locations/selectors.py`):
  ancestors, descendants and the whole tree are fetched in a constant number of
  queries whatever the depth. They use `UNION`, not `UNION ALL`, so a cycle in
  corrupt data cannot cause an endless recursion.
- Tree pages are rendered from a **flat, pre-ordered list** (`flatten_tree`), not
  by recursive template includes, which fail with `RecursionError` at ~100 levels.
- The model deliberately has **no** "allowed parent types" rule table and **no**
  materialised path / nested-set columns.

## Consequences

- The same schema serves both hierarchies in the acceptance test (see
  `tests/test_locations.py::TestHierarchyShape`).
- Subtree queries are index-assisted recursive reads. If a tenant ever has
  hundreds of thousands of locations, a materialised `path` can be added behind
  the selector API without touching callers. Maintaining one now would add a
  subtree rewrite to every move for no present benefit.
- Parent-type constraints ("an Area must sit under a Region") can later be added
  as a many-to-many on `LocationType` without a schema redesign.
- Future scoped access (Phase 4) resolves "Region 1's subtree" with
  `get_descendants(location, include_self=True)` / `get_descendant_ids([...])`,
  already composable and multi-root.
