# Checklist versioning (Phase 7)

How a checklist keeps a stable identity while its definition evolves - and why a published definition can always be
reproduced exactly as it was published. Companion documents: [checklist-lifecycle.md](checklist-lifecycle.md) (states and
transitions), [checklist-publishing.md](checklist-publishing.md) (validation and the publish service),
[version-comparison.md](version-comparison.md), [api/checklist-versions.md](api/checklist-versions.md) and
ADRs [0048](decisions/0048-version-lifecycle-and-archival.md), [0049](decisions/0049-published-definitions-are-immutable.md),
[0050](decisions/0050-version-manifest-and-integrity-hash.md).

## Template versus version

```
Organization
  └── ChecklistTemplate            the continuing identity: code (FS-001), name, category, description, general instructions
        ├── ChecklistVersion 1     a specific, reproducible definition  (published → superseded)
        │     ├── ChecklistSection (code S-001 ...)
        │     │     └── ChecklistItem (code Q-001 ..., answer_type, config, metadata)
        │     │           └── ChecklistOption (value, label)
        │     └── ChecklistRule (code R-001 ..., condition, actions)
        ├── ChecklistVersion 2     (published)
        └── ChecklistVersion 3     (draft)
```

* The **template** is what people recognise: "Restaurant Daily Safety Checklist". It never changes identity (its `code` and
  `slug` are permanent) and is *not* duplicated when the checklist changes.
* A **version** is what was actually asked at a point in time. Each version has its own UUID, a sequential `version_number`
  (unique per template, computed under a row lock, enforced by a unique constraint) and its **own** sections, items,
  options and rules. Nothing in a version points at another version's rows: a rule references questions and sections by
  *code within its own version*, an option belongs to exactly one item, and an item to exactly one section.
* Template-level text (name, description, general instructions, category, duration) is **descriptive** and may be edited
  after publication; it is not part of the version definition and not part of the integrity manifest. Everything that
  changes how a question is asked, validated, shown or required is version-scoped.
* There is **no JSON snapshot** standing in for the versions: the relational rows *are* the version. The
  [manifest](#manifest-and-integrity) is a derived, deterministic description used only for integrity, concurrency control and
  comparison.

## Identifiers

| Kind | Scope | Stability |
|---|---|---|
| Database primary key (UUID) | global | never reused; the only thing a future assignment/execution stores to reference an exact version |
| Template `code` / `slug` | organization | permanent |
| `version_number` | template | sequential; permanent for published versions |
| Section `code` (`S-001`) | version | copied unchanged into new drafts |
| Item `code` (`Q-001`, `TEMP-001`) | **version** (unique across all sections) | copied unchanged into new drafts |
| Rule `code` (`R-001`) | version | copied unchanged |
| Option `value` | item | copied unchanged; stable when the label changes |

Codes are what make versions comparable and rules portable between versions. **The same code does not mean the same
semantics**: a question can keep its code while its text, answer type or range changes, and the comparison reports exactly
that. Reusing the code of a removed question for a *different* question is permitted (it is only a label) but discouraged -
reports that group by code would silently merge the two; prefer a new code. Item codes must be unique within a version
(validated on every write and again at publication).

## Version-specific content that is copied into a new draft

Sections (order, requiredness, instructions), items (text, help text, instructions, answer type and validated configuration,
metadata, requiredness, active flag, order), options (value, label, order, active flag) and rules (condition, actions,
enabled flag, order). The dedicated cloning service (`lifecycle.clone_definition`) copies them with **new row identities** in
four bulk inserts (history rows included) after four reads - the number of queries does not grow with the checklist's size.
Because rules reference by code and codes are preserved, no reference needs remapping, and the result is validated by the
same publication validator before it can be published.

## Rolling back

Never by editing history. To restore an older definition, create a **new draft from that historical version**
(`POST .../create-draft/` or *Create draft from this version*), review it, and publish: it becomes the next version number and
supersedes the current one. The older versions remain exactly as they were.

## Manifest and integrity

`apps/checklists/manifest.py` derives a canonical manifest (sections → items → options, plus rules) from the rows and hashes
it with SHA-256. At publication the hash is **sealed** on the version (`integrity_hash`); `verify_version_integrity` re-derives
it on demand and reports `verified`, `mismatch`, `unsealed` (published before hashes existed), `unverifiable` (unknown schema) or
`draft`. A stored hash is never trusted - it is always recomputed. The same hash computed for a *draft* is the optimistic
concurrency token of a publish request. Canonical rules, included/excluded fields and schema evolution are documented in the
module and in [ADR 0050](decisions/0050-version-manifest-and-integrity-hash.md). The hash is an integrity *aid*; the immutable
rows and access control are what protect the definition.

## Immutability, honestly

A published or superseded version cannot be changed through any supported application path, and PostgreSQL itself refuses
direct writes. The layers, strongest first, and what each does *not* cover, are in
[ADR 0049](decisions/0049-published-definitions-are-immutable.md):

1. **PostgreSQL triggers** (`guards.py`, installed by a `post_migrate` handler) reject `INSERT/UPDATE/DELETE` on the
   sections, items, options and rules of a non-draft version, and any change to a published version's definition columns,
   whatever the caller - `QuerySet.update()`, `bulk_create`, the admin, raw SQL.
2. **Models** (`DefinitionGuardMixin`, `ChecklistVersion.save/delete`) raise a friendly `version_immutable` error.
3. **Services** lock the template, re-read the version and refuse anything but a draft; every write channel (builder, API,
   import, seed, admin actions) ends there.
4. **Policies, API and admin** hide and refuse edit actions; the admin marks every non-draft row read-only.

Limits: a database superuser or table owner can disable the triggers, `TRUNCATE` and a restore ignore them, and the explicit
maintenance override (`guards.allow_definition_changes`, used only to seal legacy versions) is deliberately possible for
audited maintenance. These are administrative controls (database roles, backups), not application features.

## Future assignments and executions

Phase 8 stores a reference to an exact **version** (`ChecklistVersion.pk`), never to the template. It must call
`lifecycle.assert_assignable(version)` (current published version of an *active*, non-archived template; a superseded version
is not assignable) when creating an assignment; existing assignments to a superseded version keep working. Phase 9 executions
read the version's relational rows and the Phase 6 evaluator; they never copy definitions. Archiving a template stops new
assignments and drafts but deletes nothing, so every historical reference resolves. Versions referenced by later records are
never deleted: only drafts can be deleted, and `lifecycle.protected_dependencies` is the single place future phases add their
reasons to refuse that.
