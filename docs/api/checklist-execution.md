# Checklist execution API

Base `/api/v1/`. Same authentication and response envelope (`{"data", "meta"}` / `{"error": {"code", "message", "details"}}`) as the
rest of v1. Everything is filtered by the caller's organization and location scope: invisible -> **404**, forbidden -> **403**.

| Method | Path | Purpose |
|---|---|---|
| GET | `/my-assignments/` | open work the caller may execute (with the live execution's id and status) |
| GET | `/assignments/{id}/execution/` | the assignment's live execution (404 if none / not visible) |
| POST | `/assignments/{id}/start-execution/` | start or resume: **201** first call, **200** when resuming; takes no body |
| GET | `/executions/` | list (filters `status`, `assignment`, `template`, `version`, `location`, `started_after/before`; ordering) |
| GET | `/executions/{id}/` | retrieve |
| GET | `/executions/{id}/progress/` | server-computed progress |
| GET | `/executions/{id}/responses/` | current answers (hidden-by-rules ones excluded) |
| PUT | `/executions/{id}/responses/{item_id}/` | save one answer |
| POST | `/executions/{id}/save-draft/` | save many answers (partial) |
| POST | `/executions/{id}/validate/` | dry run of submission, always 200 |
| POST | `/executions/{id}/submit/` | submit (idempotent) |
| POST | `/executions/{id}/cancel/` | `{"reason"}`; needs `execution.cancel` |
| GET | `/executions/{id}/history/` | audit timeline |

No DELETE, no PATCH, no POST to the collection.

## Saving answers

```json
PUT /api/v1/executions/{id}/responses/{item_id}/
{"value": "4.5", "not_applicable": false, "comment": "Checked at opening", "revision": 0}
```

`revision` is the response revision the client last saw (`0` for a new answer). Omitted `value` (or empty) clears the answer.
Success: `200 {"saved": [item ids], "cleared": [...], "revision": <execution revision>, "progress": {...}}`.

| Status | When | `error.details` |
|---|---|---|
| 400 | invalid answers | `{"code": "validation_failed", "errors": {item_id: [{"code","message"}]}, "saved": [...], "progress": {...}}` |
| 409 | stale revision | `{"code": "stale_revision", "conflicts": {item_id: <stored revision>}, ...}` |
| 409 | execution submitted / cancelled | `{"code": "already_submitted"}` / `{"code": "not_editable"}` |

A batch (`save-draft`) stores every valid, current answer and reports the rest; `saved` tells what was stored.
Protected fields (`status`, `organization`, `assignment`, `version`, `item`, `submitted_by`, ...) and unknown keys are rejected (400).
Item ids outside the execution's version, hidden items, media answers and malformed ids are refused (400).

## Submit and validate

`POST .../validate/` -> `{"valid": false, "problems": [{"item", "code", "section", "question", "problems": [{"code","message"}]}], "progress": {...}}`.

`POST .../submit/` -> `200` with the execution plus `"already_submitted": true|false`. Blocking problems: `400` with
`error.details = {"code": "validation_failed", "problems": {item_id: [...]}}` and nothing changed.

## Progress

`{"percent", "basis": "required"|"visible", "total", "answered", "visible_items", "required_items", "answered_required",
"unanswered_required": [item ids], "sections": {code: {"total", "answered"}}}` - computed on the server from the saved answers.

## Execution representation

```json
{"id": "...", "reference": "EXE-00007", "number": 7, "organization": "...", "status": "in_progress", "revision": 3,
 "assignment": {"id": "...", "reference": "ASG-00012", "status": "in_progress", "due_at": "..."},
 "checklist": {"template": "...", "name": "Daily Safety Checklist", "code": "...", "version": 1, "version_id": "..."},
 "location": {"id": "...", "name": "Restaurant 1", "path": "Region North / Area 1 / Restaurant 1"},
 "started_by": "...", "started_at": "...", "last_activity_at": "...", "submitted_by": null, "submitted_at": null,
 "cancelled_at": null, "cancel_reason": ""}
```
