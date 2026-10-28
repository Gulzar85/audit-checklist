# Reporting API (`/api/v1/reports/`)

Authenticated; the **active organization** must grant `report.view`. Lists use the standard envelope. Unknown request fields are errors.

| Method and path | Purpose | Needs |
|---|---|---|
| `GET /` | accessible report definitions (columns, filters, groupings, formats; no internals) | `report.view` |
| `GET /{code}/` | one definition | view |
| `POST /{code}/validate/` | validate a request -> normalized form | view |
| `POST /{code}/run/` | paginated results: `{filters, columns, sort, group_by, page, page_size<=100}` -> `{"data": [...], "meta": {count, page, pages, page_size, applied, period, generated_at, truncated}}` | view |
| `POST /{code}/export/` | `{..., "format": "csv|xlsx|pdf"}` -> a run (200 ready/failed, 202 queued for a Phase 18 job); identical requests return the same run | `report.export` |
| `GET /runs/`, `GET /runs/{id}/` | your own export history (no private file references) | view |
| `GET /runs/{id}/download/` | the private file: requester only, re-authorized now, `Cache-Control: private, no-store` | export |
| `GET/POST /saved/`, `GET/PUT/DELETE /saved/{id}/` | saved configurations (private, or organization-shared with `report.share`); only the owner edits or deletes | view |
| `POST /saved/{id}/run/`, `POST /saved/{id}/duplicate/` | run (revalidated for the caller) / private copy | view |

Request example: `{"filters": {"date_from": "2026-01-01", "date_to": "2026-03-31", "status": "submitted", "location": "<uuid>", "include_descendants": true}, "columns": ["number", "template", "score_percentage"], "sort": "-started_at"}`. Errors: 400 (validation, with a `code` such as `unsupported_filter`, `range_too_long`, `too_large`), 403, 404, 409 (`not_ready`, `expired`, `missing`).
