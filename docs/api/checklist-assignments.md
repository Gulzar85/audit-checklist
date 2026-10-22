# Checklist assignments API

Base path `/api/v1/assignments/`. Session or token authentication as the rest of v1; responses use the standard envelope
(`{"data": ..., "meta": ...}` / `{"error": {"code", "message", "details"}}`). The OpenAPI schema lists every endpoint.

Everything is filtered by the caller's organization and location scope: what you cannot see is a **404**; what you can see but
may not do is a **403**.

| Method | Path | Purpose |
|---|---|---|
| GET | `/assignments/` | list - filters `q`/`search`, `status` (incl. derived `overdue`), `priority`, `template`, `version`, `location`, `assigned_to`, `created_by`, `assigned_after/before`, `due_after/before`; ordering `ordering=-priority_rank,due_at,...`; page-number pagination |
| POST | `/assignments/` | create a draft, or issue immediately with `"issue": true` |
| GET | `/assignments/{id}/` | retrieve |
| PATCH | `/assignments/{id}/` | safe fields only: `priority`, `due_at`, `assigned_to`, `instructions` (drafts only). PUT/DELETE: 405 |
| POST | `/assignments/{id}/issue/` | draft -> assigned (revalidates) |
| POST | `/assignments/{id}/cancel/` | body `{"reason": "..."}` (required) |
| POST | `/assignments/{id}/reopen/` | cancelled -> draft |
| POST | `/assignments/{id}/change-assignee/` | `{"assigned_to": "<user id>" | null}` |
| POST | `/assignments/{id}/change-due-date/` | `{"due_at": "<iso>" | null}` |
| POST | `/assignments/{id}/change-priority/` | `{"priority": "low|normal|high|critical"}` |
| GET | `/assignments/{id}/history/` | audit timeline, newest first |
| POST | `/assignments/bulk-create/` | many locations, all-or-nothing; `dry_run` for a report |
| GET | `/assignments/summary/` | counts by status (persisted), open, overdue, due soon, by priority - respects the same filters |

## Create

```json
POST /api/v1/assignments/
{"version": "<version id>", "location": "<location id>", "priority": "high",
 "due_at": "2026-10-20T09:00:00+05:00", "instructions": "Opening checks", "assigned_to": null, "issue": false}
```

Returns `201` with the assignment. `status`, `organization`, `created_by` and `number` are never accepted.

## Representation

```json
{"id": "...", "reference": "ASG-00007", "number": 7, "organization": "...",
 "status": "assigned", "effective_status": "overdue", "is_overdue": true, "priority": "high",
 "instructions": "...", "due_at": "...",
 "checklist": {"template": "...", "name": "Restaurant Daily Safety Checklist", "code": "...", "version": 1, "version_id": "..."},
 "location": {"id": "...", "name": "Restaurant 1", "path": "Region North / Area 1 / Restaurant 1"},
 "assigned_to": {"id": "...", "email": "...", "name": "..."},
 "created_by": "...", "created_at": "...", "issued_at": "...", "cancelled_at": null, "cancel_reason": ""}
```

## Bulk

```json
POST /api/v1/assignments/bulk-create/
{"version": "...", "locations": ["...", "..."], "include_descendants": false, "priority": "normal",
 "due_at": null, "issue": true, "dry_run": false}
```

`201` `{"created": true, "count": n, "targets": [{"location", "name", "ok", "errors", "assignment", "reference"}]}`. When any
target fails nothing is created and the response is `400` with the same `targets` in `error.details` (each failed entry says
why). `dry_run: true` always returns `200` with the report. Limit: 200 locations.

## Errors

| Status | When |
|---|---|
| 400 | validation: bad field, past due date, ineligible version/location/user, protected field, invalid transition (`details.code`: `invalid_transition`, `invalid_due`, `invalid_assignee`, `version_not_assignable`, `inactive_location`, ...) |
| 401/403 | not authenticated / not permitted |
| 404 | not visible to the caller |
| 409 | repeat or race: `already_issued`, `already_cancelled`, a duplicate open assignment (`duplicate`) |

