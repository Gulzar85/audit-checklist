# API: checklist versions

Base path `/api/v1/`. Success responses use the standard envelope (`{"data": ..., "meta": {}}`); errors use
`{"error": {"code", "message", "details"}}`. Authentication is the session; authorization is the checklist policies
(`checklist.view | create | update | publish | manage`, organization-level). A checklist or version outside the caller's
organizations is **404** everywhere (existence is never revealed); visible-but-forbidden is **403**. Every lifecycle action calls
the same service the builder uses (`apps/checklists/lifecycle.py`) - there is no business logic in the views or serializers.

Lifecycle, statuses and transitions: [../checklist-lifecycle.md](../checklist-lifecycle.md). Validation rules:
[../checklist-publishing.md](../checklist-publishing.md).

## The version resource

```json
{"id": "…", "template": "…", "version_number": 2, "status": "published", "change_summary": "Tighten range",
 "based_on": 1, "created_by": "…", "created_at": "…", "published_at": "…", "published_by": "…", "superseded_at": null,
 "integrity_hash": "sha256:v1:9c1f…", "section_count": 3, "item_count": 4}
```
`status` is `draft | published | superseded`. `based_on` is the version *number* this one was copied from.

| Method | Path | Permission | Notes |
|---|---|---|---|
| `GET` | `/checklists/{id}/versions/` | view | the history, newest first, with `section_count` / `item_count` (not paginated) |
| `POST` | `/checklists/{id}/versions/` | update | create a draft: `{"change_summary"?, "source"?}` - a copy of `source` (a version id of this checklist) or of the current published version; `201` |
| `POST` | `/checklists/{id}/versions/new/` | update | the original alias of the above |
| `GET` | `/checklists/{id}/versions/compare/?from=&to=` | view | compare two versions (ids or numbers), see below |
| `GET` | `/checklist-versions/{version_id}/` | view | the version, plus `integrity` (`{"status", "verified"}`), `assignable` and `publication_warnings` |
| `POST` | `/checklist-versions/{version_id}/validate/` | update or publish | the publication report (always `200`; `valid` says whether it would publish) |
| `POST` | `/checklist-versions/{version_id}/publish/` | publish | `{"change_summary"?, "expected_hash"?}`; `200` + the version |
| `POST` | `/checklist-versions/{version_id}/create-draft/` | update | `{"change_summary"?}` - a new draft copied from **this** version (rollback); `201` |
| `DELETE` | `/checklist-versions/{version_id}/` | update | discard a **draft** (`204`); published/superseded -> `400`, the only version -> `400` |
| `GET` | `/checklist-versions/{version_id}/manifest/` | view | the canonical manifest, its current `hash` and the stored `integrity_hash` |
| `GET` | `/checklist-versions/{version_id}/integrity/` | view | re-derives the hash: `verified | mismatch | unsealed | unverifiable | draft` |
| `POST` | `/checklists/{id}/archive/` `.../restore/` | manage | unchanged (Phase 4); an archived checklist refuses new drafts |

Edits of a published or superseded version's sections, items, options and rules (`/checklists/{id}/items/...`,
`/rules/...`) are refused with `400 version_immutable`; create a draft instead.

### Validate

```http
POST /api/v1/checklist-versions/{id}/validate/
```
```json
{"data": {"valid": false, "version": 2, "draft_hash": "sha256:v1:…", "stats": {"sections": 3, "items": 4, "options": 8, "rules": 1},
          "errors": [{"level": "error", "code": "empty_section", "message": "Section “Food Storage” has no active items.", "subject": "section:S-002"}],
          "warnings": [{"level": "warning", "code": "no_instructions", "message": "No checklist instructions have been provided.", "subject": "template"}]}}
```
Codes and meanings: [../checklist-publishing.md](../checklist-publishing.md#what-blocks-publication-errors).

### Publish

```http
POST /api/v1/checklist-versions/{id}/publish/
{"change_summary": "Tighten the refrigerator range", "expected_hash": "sha256:v1:…"}
```

* `expected_hash` (optional, recommended) is the `draft_hash` of the last validation. If the draft changed since: **409**.
* Atomic and serialised per checklist; the previously published version becomes `superseded` in the same transaction.

| Outcome | Status | Body |
|---|---|---|
| Published | `200` | the version |
| Blocking validation errors | `400` | `error.details.report` = the full report (typed), `error.details.__all__` = messages, `code: "publication_blocked"` |
| Draft changed since `expected_hash` | `409` | `error.code: "CONFLICT"`, `error.details.code: "stale_draft"` |
| Already published (repeat / concurrent request) | `409` | `error.details.code: "already_published"` - nothing was published twice |
| No `checklist.publish` | `403` | |
| Unknown / other organization's version | `404` | |

### Create a draft / discard

`409` is *not* used for "a draft already exists": that is `400` with `details.code: "draft_exists"` (the Phase 4 contract). A
race that slips past the check (two requests at the same instant) is serialised by the row lock, so the loser also gets
`draft_exists`. `DELETE` of the only version returns `400 only_version` (delete the draft *checklist* instead); published and
superseded versions are never deleted.

### Compare

```http
GET /api/v1/checklists/{id}/versions/compare/?from=1&to=2&impact=behavior
```
`from` / `to` are version ids or numbers *of this checklist*. Optional filters: `category` (`section|item|option|rule`), `kind`
(`added|removed|modified|reordered`), `impact` (`behavior|wording|structure`); an unknown filter value is `400`.
```json
{"data": {"identical": false, "from": 1, "to": 2,
          "counts": {"total": 2, "by_category": {"section": 0, "item": 1, "option": 0, "rule": 1},
                     "by_kind": {"added": 0, "removed": 0, "modified": 2, "reordered": 0},
                     "by_impact": {"behavior": 2, "wording": 0, "structure": 0}},
          "changes": [{"category": "item", "kind": "modified", "code": "FRIDGE-TEMP", "label": "Record refrigerator temperature.",
                       "summary": "“Record refrigerator temperature.”: setting “max” changed: 10 → 8.", "impact": "behavior",
                       "field": "config.max", "before": 10, "after": 8, "context": "Food Storage"}]}}
```
`counts` always describes the unfiltered comparison; `changes` honours the filters. A version of another checklist or
organization, or an unknown id/number, is `404`; missing `from`/`to` is `400`.

### Integrity

`integrity` and `manifest` let clients and auditors check that a published definition is exactly what was published:
`manifest.hash` (re-derived now) must equal `manifest.integrity_hash` (sealed at publication). See
[../checklist-versioning.md](../checklist-versioning.md#manifest-and-integrity).

## Authorization matrix

| Action | Needs | Notes |
|---|---|---|
| read history, detail, manifest, integrity, compare | `checklist.view` | drafts are readable too (read-only) |
| create a draft, discard a draft, validate | `checklist.update` (validate also with `checklist.publish`) | |
| publish | `checklist.publish` | status-independent check, so a repeated request is a `409`, not a `403` |
| archive / restore | `checklist.manage` | |

A role granted only at a location (scope) does not open definitions: checklist definitions are organization-level, so the
version endpoints answer `404` for such a caller.

## Future assignment and execution

Phase 8 references a **version id** (never a template id) and calls `lifecycle.assert_assignable(version)`: only the current
published version of an active, non-archived checklist is assignable; superseded versions remain readable and keep existing
references. Executions read the version's rows and the Phase 6 evaluator.
