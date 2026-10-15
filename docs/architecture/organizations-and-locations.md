# Organizations and the location hierarchy

Phase 1 delivers the organizational foundation every later phase references:
`Organization`, `LocationType`, `Location`. Checklists, assignments, executions,
findings, corrective actions, scoring and reports belong to later phases and are
deliberately absent.

```
Organization
 ├── LocationType  (each organization's own vocabulary; or platform-defined, org = NULL)
 └── Location ──parent──> Location ──parent──> Location ...   (any depth)
        └── location_type ──> LocationType
```

Decisions: [0009](../decisions/0009-organization-identity.md) ·
[0010](../decisions/0010-generic-location-hierarchy.md) ·
[0011](../decisions/0011-organization-lifecycle-single-status.md) ·
[0013](../decisions/0013-hierarchy-integrity-in-three-layers.md) ·
[0014](../decisions/0014-deactivate-not-delete.md) ·
[0016](../decisions/0016-platform-defined-location-types.md)

## 1. Organization

The tenant root. Only fields with architectural value were kept.

| Group | Fields | Why |
| ----- | ------ | --- |
| Identity | `id` (UUID), `name`, `code`, `slug`, `legal_name`, `description` | `code` is the immutable business key; `slug` a unique handle; URLs use the UUID ([ADR 0009](../decisions/0009-organization-identity.md)) |
| Lifecycle | `status` (`active` / `inactive` / `suspended`) | the *single* lifecycle authority; `is_active` is derived ([ADR 0011](../decisions/0011-organization-lifecycle-single-status.md)) |
| Branding | `logo`, `favicon`, `primary_color` | validated uploads; brand colour `#RRGGBB` |
| Contact | `website`, `email`, `phone`, address lines, `city`, `state`, `postal_code`, `country` | basic business details |
| Settings | `timezone` (IANA), `currency` (ISO 4217), `language`, `date_format` (fixed choices) | typed, validated, blank = platform default (`effective_*` properties) |

**Considered and rejected:** a catch-all JSON `configuration` column (the draft had
one; nothing used it, and a JSON blob is how settings become unvalidated and
unqueryable). Future settings should be added as typed fields, or a dedicated
settings model when a group grows large. `Location.metadata` (JSON) is kept because
free-form, non-queryable attributes are exactly what that field is for.

**Lifecycle.** `status` changes only through `organizations.services` (locked, logged,
historised). Inactive/suspended organizations disappear from their members' view
(404 everywhere) and refuse hierarchy writes - for everyone - until reactivated.
Nothing is deleted. Only platform staff change status.

**Branding uploads** (`organizations/validators.py`): extension allow-list (no SVG,
scripts, HTML or executables), 2 MB cap, **real image verification with Pillow**
(not the client's MIME type), a 4096 px per-side cap (decompression-bomb guard), and
a **server-generated filename** (`organizations/<id>/logo/<uuid>.png`) so client
names, double extensions and path tricks never reach storage. Files use the default
Django storage (the Phase 0 abstraction; S3/Azure later with no code change).

## 2. LocationType

`organization` (nullable), `name`, `code`, `description`, `sort_order`, `is_active`.
A `NULL` organization is a **platform-defined** type usable by everyone, editable by
platform staff only ([ADR 0016](../decisions/0016-platform-defined-location-types.md)).
Codes are unique per organization (and among platform types). Deactivating a type
keeps existing locations; it just cannot be newly assigned.

## 3. Location

`organization`, `parent`, `location_type` (required), `name`, `code`, `description`,
`is_active`, address, `latitude`/`longitude` (both or neither), `timezone`
(blank inherits the organization's), `metadata`. Codes are unique **within the
organization** and normalised (`kh-001` → `KH-001`).

**No slug:** public URLs use the UUID, so a slug would add a uniqueness rule for no benefit.

### Hierarchy operations (all in `locations/selectors.py`)

| Function | Returns |
| -------- | ------- |
| `get_root_locations(org)`, `get_children(loc)` | querysets |
| `get_descendants(loc, include_self=)` | a composable queryset (one SQL statement) |
| `get_ancestors(loc)` | list, root → parent (one query) |
| `get_descendant_ids(roots, include_roots=)` / `get_ancestor_ids(locs)` | id sets for **many** roots in one query (the building block for Phase 4 scoped access) |
| `get_location_tree(queryset)` → `flatten_tree(roots)` | nested `TreeNode`s / a flat, recursion-free row list |
| `get_locations_for_organization`, `get_active_locations` | querysets |
| `find_integrity_problems(org=None)` | audit used by `validate_location_tree` |

Recursion is a PostgreSQL recursive CTE using `UNION`; query count is constant in
tree size and depth (asserted in tests).

### Operations (all in `locations/services.py`)

`create_location`, `update_location` (also performs a move when `parent` changes),
`move_location`, `activate_location`, `deactivate_location(cascade=)`, and
`create_/update_/activate_/deactivate_location_type`. Each is transactional, takes the
organization row lock, writes `simple_history` with the acting user, and records an
`ActivityLog` event: `location.created | updated | parent_changed | activated |
deactivated` (and `location_type.*`, `organization.*`).

### Integrity

Self-parent, cycles, cross-organization parent, cross-organization type and organization
changes are rejected by the **model**, the **service layer** and **PostgreSQL itself**
(CHECK constraints + triggers), with an organization lock + advisory lock for races
([ADR 0013](../decisions/0013-hierarchy-integrity-in-three-layers.md)).

### Deactivation ([ADR 0014](../decisions/0014-deactivate-not-delete.md))

Locations are deactivated, never deleted. An active location cannot sit beneath an
inactive parent, so deactivating a parent needs `cascade=True` (the UI asks for
confirmation and shows the descendant count); reactivation works top-down; new
locations cannot be created or moved under an inactive parent. All hierarchy foreign keys are `PROTECT`.

## 4. Query strategy and indexes

| Table | Index / constraint | Serves |
| ----- | ------------------ | ------ |
| Organization | unique `code`, unique `slug` | identity; import key |
|  | `(status, name)` | lists/filters by lifecycle, ordered by name |
| LocationType | unique `(organization, code)` where organization is set; unique `(code)` where it is null | per-organization and platform code uniqueness |
| Location | unique `(organization, code)` | code lookup, import key, uniqueness |
|  | `(organization, is_active)` | "active locations of an organization" |
|  | `(organization, parent)` | roots and children within an organization |
|  | `(organization, location_type)` | filter by type |
|  | `(organization, name)` | name-ordered listings |
|  | FK indexes on `parent`, `location_type`, `organization` (Django) | joins, `PROTECT` checks, recursive CTE steps |

The draft's single-column `is_active`/`status`/`parent` indexes were dropped: each was
covered by a composite or an FK index and low-selectivity. No trigram/GIN index yet:
`icontains` search over an organization's locations is cheap at the expected scale; add
`pg_trgm` if a tenant reaches tens of thousands of rows.

**N+1 control.** Lists use `select_related`; the API list annotates `children_count` with
a correlated subquery (no `GROUP BY`); trees are built from one query plus one ancestor
query. `test_*query_count*` tests assert counts do not grow with row count or depth.

**Concurrency.** Tree writes take `SELECT ... FOR NO KEY UPDATE` on the organization row,
serialising administrators of one tenant. Field edits that do not touch the hierarchy are
last-write-wins with full history (no optimistic-lock token in Phase 1).

## 5. Where things live

```
apps/organizations/  models, services, selectors, policies, forms, filters, tables,
                     views, urls, admin, resources, serializers, validators
apps/locations/      models, services, selectors, policies, forms, widgets, filters,
                     tables, views, urls, admin, resources, serializers,
                     management/commands/{validate_location_tree,seed_demo_data}
apps/api/v1/         organizations.py, locations.py (ViewSets), urls.py
apps/core/           mixins (PolicyMixin), forms (design-system mixins), validators,
                     scoping, services (log_activity, apply_changes, stamp_history)
```
