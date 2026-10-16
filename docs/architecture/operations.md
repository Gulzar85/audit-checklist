# Operating Phase 1

## Migrations

| Migration | What it does |
| --------- | ------------ |
| `organizations.0003` | drops the unused `configuration` JSON, adds `primary_color`, validators/choices, status/slug/country CHECK constraints, UUID-based upload paths |
| `locations.0003` | required `location_type`, `PROTECT` foreign keys, code/coordinate/country CHECKs, index clean-up. Pre-flight: refuses with a clear message if any location has no type, and normalises existing codes first |
| `locations.0004` | PostgreSQL integrity triggers (see [ADR 0013](../decisions/0013-hierarchy-integrity-in-three-layers.md)); fully reversible |

```powershell
.\venv\Scripts\python manage.py migrate
.\venv\Scripts\python manage.py makemigrations --check     # must report no changes
```

Earlier migrations are never edited. The location triggers are created by migration, never by hand.

**Checklist immutability triggers** (ADR 0049) are different: they are installed by a `post_migrate` handler
(`apps/checklists/guards.py`), idempotently, after *every* `migrate` - so they exist in every environment and test database
even though migration files are not committed. `ChecklistVersion` gained lifecycle columns in `checklists.0005`
(a data step marks older published versions as superseded); after migrating an existing database run
`seal_checklist_versions` once so versions published before Phase 7 get an integrity hash, then `verify_checklist_integrity`.

## Management commands

```powershell
# Audit the hierarchy; exits non-zero on any problem (use it after a restore or in CI)
.\venv\Scripts\python manage.py validate_location_tree [--organization CODE]

# Demo data (development only; refuses when DJANGO_ENVIRONMENT is production)
.\venv\Scripts\python manage.py seed_demo_data

# Checklist versioning (Phase 7)
.\venv\Scripts\python manage.py seal_checklist_versions [--organization SLUG]       # idempotent; maintenance override, logged
.\venv\Scripts\python manage.py verify_checklist_integrity [--organization SLUG] [--strict]   # non-zero exit on mismatch
.\venv\Scripts\python manage.py seed_demo_versioning --organization SLUG            # v1 -> v2 walkthrough on a generic checklist
```

`seed_demo_data` is idempotent and creates two clearly-labelled organizations with different
vocabularies, through the same services as the application, and **no users or credentials**:

```
DEMO     Global → Region A → Area A → Location A, Location B   (+ Region B → Area B → Location C)
DEMO_BU  Business Unit 1 → Division 1 → Facility 1 → Department 1, Department 2
```

Production never receives demo records automatically; the command is the only route and it
refuses there. Create a platform operator with `createsuperuser`; give members access by adding
an `OrganizationMembership` (admin) and the model permissions they need.

## Import / export

Admin → *Organizations / Location types / Locations* → **Import** / **Export**. Columns:

* organizations: `code, name, slug, legal_name, description, status, website, email, phone, address_line1, address_line2, city, state, postal_code, country, timezone, currency, language, date_format, primary_color`
* location types: `organization (code; blank = platform), code, name, description, sort_order, is_active`
* locations: `organization (code), code, name, location_type (code), parent (code), is_active, description, address..., country, latitude, longitude, timezone`

Always run the dry-run preview first. See [ADR 0018](../decisions/0018-safe-import-export.md).

## Verification checklist

```powershell
.\venv\Scripts\python manage.py check
.\venv\Scripts\python manage.py check --deploy            # with DJANGO_SETTINGS_MODULE=config.settings.production
.\venv\Scripts\python manage.py makemigrations --check
.\venv\Scripts\python manage.py migrate --check
.\venv\Scripts\python -m pytest
.\venv\Scripts\python manage.py spectacular --validate --fail-on-warn
npm run css:build                                         # after adding Tailwind classes
```

## Test layout

| File | Covers |
| ---- | ------ |
| `test_organizations.py` | identity, validation, lifecycle, history, logging |
| `test_location_types.py`, `test_locations.py` | types, services, deactivation/cascade, hierarchy shapes |
| `test_hierarchy_integrity.py` | self-parent, cycles, cross-org parent/type at service, model **and database** level; a real two-thread race |
| `test_tree_selectors.py` | roots/children/ancestors/descendants, tree assembly, query counts, depth |
| `test_api_organizations.py`, `test_api_locations.py` | API auth, permissions, isolation, filters, pagination, validation, actions, tree |
| `test_web_organizations.py`, `test_web_locations.py` | pages, forms, HTMX fragments, permissions, error states |
| `test_tenant_isolation.py` | exhaustive cross-tenant matrix, leak sweeps, suspended organizations |
| `test_policies.py` | policy matrix over every user kind |
| `test_branding.py` | upload type/size/content/filename security across all entry points |
| `test_import_export.py` | atomicity, ordering, cycles, cross-tenant rows |
| `test_admin.py`, `test_commands.py` | admin safety, audit command, demo seed |
| `test_admin_theme.py` | Jazzmin configuration quality, no third-party requests, admin CSP and inline-script hash drift |
| `test_ui_quality.py` | strict-CSP compatibility, accessibility structure, PWA shell |

Tests run against PostgreSQL (triggers, CTEs and locks are part of what is being tested).

## Known limits (deliberate, for later phases)

* Imports are an admin feature; no self-service import UI yet.
* Field edits are last-write-wins with history (no optimistic-lock token).
* Roles/scopes (per-organization and per-subtree grants) arrive in Phase 4; today permissions are
  Django model permissions plus membership.
* The HTML tree is practical to ~150 levels (browser nesting limit); the model, API and list view have no limit.
