# 0049. Published definitions are immutable: services, models and PostgreSQL triggers

- **Status:** Accepted
- **Date:** 2026-10-16
- **Extends:** [0013](0013-hierarchy-integrity-in-three-layers.md) (same layered approach)

## Context
"A published version is a stable, reproducible definition" is the central invariant of the checklist platform. Hiding buttons
or checking a status in one service is not enough: `QuerySet.update()`, bulk operations, cascaded deletes, the admin and raw SQL
all bypass `save()` overrides and services.

## Decision
Four layers, each with a distinct job:

1. **Services** (`services._editable`, `lifecycle`) - lock the template row, re-read the version and refuse anything but a draft of
   a live template, with a friendly `version_immutable` error. Every write channel (builder, API, import, seed, admin actions)
   ends here; they also provide locking, audit and history.
2. **Models** - `DefinitionGuardMixin` on sections, items, options and rules and `ChecklistVersion.save/delete` refuse writes to
   a non-draft version (`ValidationError`), covering forms, the admin and direct ORM calls.
3. **PostgreSQL triggers** (`guards.py`) - `BEFORE INSERT/UPDATE/DELETE` on the four child tables raise `check_violation`
   unless the owning version is a draft; a trigger on versions allows `draft -> published` and `published -> superseded` only,
   freezes the definition and publication columns of non-draft versions, and refuses deleting them. These stop `QuerySet.update()`,
   `bulk_create/update`, cascades and raw SQL. They are (re)installed idempotently by a `post_migrate` handler, so they exist after
   every `migrate` regardless of which migration files a checkout has (migrations are not committed in this project).
4. **Policies, API, admin** - edit actions are not offered, endpoints answer `400/404`, and every admin class and inline marks
   non-draft rows read-only.

The **only** override is `guards.allow_definition_changes()` - explicit, transaction-scoped, applied to the application and
database guards together - used by `seal_checklist_versions` to record the integrity hash of versions published before hashes
existed. It is logged (`checklist_version.sealed`) and never enabled by application code on its own.

## Boundaries and known limits (stated, not hidden)
* A database **superuser or the table owner** can drop or disable the triggers; `TRUNCATE` does not fire row triggers; restoring a
  backup replaces data. These are controlled with database roles, privileges and backup procedures - not by the application.
* `simple_history` tables are append-only by design and are not guarded.
* The integrity hash (ADR 0050) *detects* a change that got past these layers (`verify_checklist_integrity`); it does not prevent one.
* Triggers add a small per-row cost on definition writes (a version-status lookup); cloning a large checklist stays in bulk
  inserts, so the cost is bounded.

## Consequences
* Tests attack each layer separately (`tests/test_versioning_immutability.py`): services, models, ORM bulk operations, raw SQL,
  admin (including inlines), import, web and API.
* Any future code that must legitimately change a published row (a data migration, a repair) has to use the maintenance override
  and say so; there is no silent path.
