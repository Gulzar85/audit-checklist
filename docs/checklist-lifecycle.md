# Checklist lifecycle

Every status change of a checklist version goes through `apps/checklists/lifecycle.py`; no view, serializer, admin action or
import decides lifecycle rules on its own. The allowed transitions are a table (`ALLOWED_TRANSITIONS`) and anything not in it is
refused (`assert_transition`).

## Version statuses

| Status | Meaning | Editable | Assignable (new assignments) | Deletable |
|---|---|---|---|---|
| `draft` | A working copy. At most **one** per template. | yes - sections, items, options, answer configuration, rules, instructions, order | no | yes (*discard*) |
| `published` | The current, approved definition. **Exactly one** per template at a time (partial unique index). | **no** - immutable | **yes**, if the template is active | no |
| `superseded` | Was published, a newer version replaced it. | no - immutable | **no** (policy below) | no |

```
DRAFT ──publish──▶ PUBLISHED ──(a newer version is published)──▶ SUPERSEDED
  │
  └──discard──▶ (deleted)
```

Not allowed, and refused by the service, the model *and* the database: `published → draft`, `superseded → published`, editing a
non-draft version, deleting a non-draft version, changing a published version's publication metadata.

### Why there is no `archived` *version* status
Archiving is a property of the **template** (`TemplateStatus.ARCHIVED`). A second, version-level flag would let the two
contradict each other ("published version of an archived template") and would force every reader to reconcile them. One source
of truth: an archived template refuses new drafts, publications and assignments; all its versions stay readable and untouched.
([ADR 0048](decisions/0048-version-lifecycle-and-archival.md))

## Template statuses

| Status | Meaning |
|---|---|
| `draft` | Never activated. Can be deleted only if it never had a published version. |
| `active` | Has a published version; the version is assignable. Activation is explicit (`Activate`). |
| `archived` | Kept for history. No edits, no new drafts, no publication, no assignment. Reversible only by an explicit, authorized *Restore*. |

## Operations

| Operation | Service | Permission | Effect |
|---|---|---|---|
| Create template (+ empty draft v1) | `services.create_checklist_template` | `checklist.create` | |
| Edit the draft | `services.*` (sections, items, options, rules) | `checklist.update` | refused unless the version is a draft of a live template |
| Create a draft | `lifecycle.create_draft(template, source=None)` | `checklist.update` | copy of the current published version (or of a chosen non-draft version); refused if a draft exists or the template is archived |
| Validate | `lifecycle.run_validation` | `checklist.update` or `checklist.publish` | structured report; audit entry |
| Publish | `lifecycle.publish_version` | `checklist.publish` | validated, atomic, supersedes the previous published version |
| Discard a draft | `lifecycle.discard_draft` | `checklist.update` | deletes the draft and its children; refused for the only version |
| Compare | `lifecycle.compare_versions` | `checklist.view` | read-only |
| Archive / restore template | `services.archive_checklist_template` / `restore_checklist_template` | `checklist.manage` | |
| Seal a legacy version | `lifecycle.seal_version` (management command) | operator | records the integrity hash of a version published before hashes existed |

Permissions are the existing Phase 3 ones (`checklist.view|create|update|publish|delete|manage`); no new permission was needed:
*version create/validate/compare* map to `update`/`update|publish`/`view`, *archive* to `manage`. They are organization-level
(definitions belong to an organization, not a location).

## Superseded versions: the policy

When a version is published the previously published version becomes `superseded` **in the same transaction**. A superseded
version

* remains fully readable (history, detail, preview, comparison, API) and is never deleted or rewritten;
* is **not assignable** for new work (`lifecycle.assert_assignable`), so operations always start from the current definition;
* keeps every reference made while it was current (assignments, later executions). Nothing is migrated automatically.

A future phase that needs "keep using the old version until the current assignment ends" decides that in the assignment
workflow; the version rows are already preserved to allow it.

## Discarding a draft

Allowed for a draft only, and only if another version exists (to abandon the first draft of a never-published checklist, delete
the draft *checklist*). `lifecycle.protected_dependencies(version)` is the single hook through which later phases refuse a
deletion. After a discard the next draft reuses the discarded number (numbers are assigned as `max(existing) + 1`; a discarded
draft was never published or referenced).

## Concurrency

Every lifecycle operation, and every structure edit, first takes `SELECT ... FOR UPDATE` on the template row, so:

* two publications of one draft: the second waits, then finds the version already published and gets a **conflict** (`409`,
  `already_published`) - one publication event, no duplicates;
* two draft creations: the second finds the first draft (`draft_exists`, `400` - the Phase 4 contract); the partial unique
  index (one draft per template) and the unique `(template, version_number)` index are the safety net behind the lock (a loser
  that somehow slips past it is a `409` conflict);
* an edit racing a publication: whichever takes the lock first wins; an edit after publication is refused (immutable), a
  publication after an edit validates the edited draft;
* a draft that changed after the client validated it: the publish request carries `expected_hash` (the draft's manifest hash)
  and a mismatch is a **conflict** (`stale_draft`) - the user re-validates instead of publishing something unreviewed.

## Errors users see

| Situation | Message | API |
|---|---|---|
| Editing a non-draft | "This checklist version is already published and cannot be edited. Create a new draft version to make changes." | 400 |
| Blocking validation errors | "The draft contains validation errors and cannot be published." (+ the list) | 400 |
| Already published (repeat request) | "Version N is already published; nothing was changed." | 409 |
| Draft changed since validation | "The draft changed after it was last validated. Review the changes and validate again before publishing." | 409 |
| A draft already exists | "This checklist already has a draft version. Publish or discard it first." | 400 |
| Archived template | "The checklist is archived and cannot be used to create new versions." | 400 |
| Versions of different checklists | "The selected versions do not belong to the same checklist template." | 400 |
| No permission | 403; a checklist outside the caller's organizations is 404 (existence not revealed) | |
