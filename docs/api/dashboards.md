# Dashboards API (`/api/v1/dashboards/`)

Authenticated; the active organization must grant `dashboard.view` and `report.view`. Values are calculated on request ("live").

| Method and path | Purpose |
|---|---|
| `GET /` | dashboards the caller may use, `can_manage` |
| `GET /kpis/` | the KPI definitions behind widgets the caller may use |
| `GET /{code}/` | the dashboard and its effective layout (`layout_source`: personal / organization / default) |
| `POST /{code}/data/` | `{"filters": {...}, "widgets": [...]}` (both optional; at most 20 widgets) -> `{"dashboard", "filters", "period": {"from", "to"}, "bucket", "widgets": [...]}` |
| `GET /widgets/{widget}/data/?date_from=&date_to=&location=&include_descendants=&template=&bucket=` | one widget |
| `GET/PUT/DELETE /{code}/layout/` | personal layout: `{"widgets": [{"code": "...", "hidden": false}]}`; DELETE resets |
| `PUT/DELETE /{code}/layout/shared/` | organization default (`dashboard.manage`) |

Widget payload: `code, kind, title, description, status (ok | empty | not_comparable | error | forbidden), not_applied, period, generated_at, freshness: "live"`; KPI widgets add `value, unit, previous, change_pct, comparison, note, definition {numerator, denominator, version}, drill`; charts add `metrics [{key,label,unit}], series [{label, key, values, drill}], bucket, truncated, drill`. Errors: 400 (unknown filter / widget, invalid range), 403, 404.
