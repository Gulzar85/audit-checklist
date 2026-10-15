# REST API (v1) - organizations and locations

Decision: [0017](../decisions/0017-api-as-thin-adapter.md). Interactive docs: `/api/docs/`;
schema: `/api/schema/` (validates with `manage.py spectacular --validate --fail-on-warn`).

Authentication is the Phase 0 session foundation (Entra ID tokens come later); every endpoint
requires an active, authenticated user. Responses use the standard envelope:

```json
{ "data": ..., "meta": { "count": 12, "next": "...", "previous": null } }
{ "error": { "code": "VALIDATION_ERROR", "message": "...", "details": { "code": ["..."] } } }
```

## Endpoints

| Method & path | Purpose | Notes |
| ------------- | ------- | ----- |
| `GET /api/v1/organizations/` | list visible organizations | filter, search, order, paginate |
| `POST /api/v1/organizations/` | create | platform staff |
| `GET/PUT/PATCH /api/v1/organizations/{id}/` | read / update | `status` and `code` are not editable |
| `POST /api/v1/organizations/{id}/status/` | `{"status": "active|inactive|suspended"}` | platform staff |
| `GET/POST /api/v1/location-types/` | list / create | platform types need platform staff |
| `GET/PUT/PATCH /api/v1/location-types/{id}/` | read / update | |
| `POST /api/v1/location-types/{id}/activate/`, `.../deactivate/` | lifecycle | |
| `GET/POST /api/v1/locations/` | list / create | |
| `GET/PUT/PATCH /api/v1/locations/{id}/` | read / update | sending `parent` moves it |
| `POST /api/v1/locations/{id}/move/` | `{"parent": "<id>" | null}` | cycle / cross-tenant safe |
| `POST /api/v1/locations/{id}/activate/` | reactivate | parent must be active |
| `POST /api/v1/locations/{id}/deactivate/` | `{"cascade": true|false}` | refused with active children unless `cascade` |
| `GET /api/v1/locations/tree/` | nested hierarchy | same filters as the list; not paginated |

There is intentionally **no DELETE** (405): resources are deactivated ([ADR 0014](../decisions/0014-deactivate-not-delete.md)).

## Filters

`organizations`: `status`, `is_active`, `country`, `search`/`q`, `ordering` (`name`, `code`, `status`, `created_at`).

`location-types`: `organization`, `status`, `is_active`, `search`.

`locations`: `organization`, `location_type`, `parent`, `root=true` (top level only), `status`
(`active|inactive`), `is_active`, `country`, `city`, `search`/`q` (name, code, city), `ordering`.
Relation filters only accept ids the caller can see (a foreign id is a 400, never a result).
Pagination: `page`, `page_size` (default 25, max 100).

## Status codes

| Code | Meaning |
| ---- | ------- |
| 200 / 201 | ok / created |
| 400 | validation (fields in `error.details`), invalid filter value, hierarchy rule violated |
| 401 / 403 | not authenticated / authenticated but not allowed |
| 404 | missing, malformed id, **or outside your tenants** |
| 405 | method not offered (DELETE) |

## Examples

```
POST /api/v1/locations/
{ "organization": "<org id>", "location_type": "<type id>", "parent": "<parent id>",
  "name": "Leeds Central", "code": "leeds-1", "country": "gb" }
→ 201 { "data": { "id": "...", "code": "LEEDS-1", "country": "GB", "is_active": true, ... } }

POST /api/v1/locations/{id}/move/   { "parent": "<its own descendant>" }
→ 400 { "error": { "code": "VALIDATION_ERROR", "details": { "parent": ["A location cannot be moved beneath one of its own descendants."] } } }

GET /api/v1/locations/tree/?search=leeds
→ 200 { "data": [ { "name": "Global", "matched": false, "children": [ ... { "name": "Leeds Central", "matched": true } ] } ] }
```

Every resource carries a stable `id` and `created_at` / `updated_at`, which is what a later
offline-sync engine needs. Throttling and the envelope are inherited from Phase 0.
