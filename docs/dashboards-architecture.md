# Dashboards and analytics (Phase 20)

Interactive summaries over **real, authorized data**, computed live through **one calculation path: the Phase 19 report registry**. Detailed rows and exports stay in Phase 19.

## Audit findings
* No dashboard app or chart library existed (`static/vendor`: alpine, htmx, lucide); strict CSP (no inline scripts/styles); theme tokens in `tailwind/input.css` / `theme-defaults.css`; Tailwind is built with the existing `npm run css:build`.
* `dashboard.view` already existed; `dashboard.manage` was added (organization-wide) for shared default layouts.
* Phase 19 provides scoped, validated aggregation (`apps/reporting/services/execution.run`, `filters.clean`, `reports/_base.apply_common`). Phase 20 added **summary reports** to that registry (`execution_summary`, `score_trend`, `score_distribution`, `findings_summary`, `actions_summary`, `workflow_summary`, `evaluation_summary`) and day/week/quarter buckets, so the same numbers are available as reports and exports.
* Cache: only a per-process `LocMemCache` exists. **No caching was added**: metrics are calculated on demand ("live"); every widget query is grouped and bounded. No Phase 18 refresh task was registered (no measured need).
* `accounts.UserPreference` is a string-only key/value store, so layouts use a small dedicated model.

## Architecture (`apps/dashboards`)
* `kpis.py` - **KPI registry**: each KPI points at a Phase 19 report, a grouping and a metric column, with its numerator/denominator text, unit, empty-scope rule and calculation version. No formulas, no SQL.
* `catalog.py` - **widget registry** (`kpi`, `trend`, `bars`, `donut`, `table`) and the **dashboard catalogue** (`overview`, `operations`, `scores`, `findings`, `actions`, `workflows`, `evaluations`, `locations`).
* `services/metrics.py` - `clean_context` (shared filters: date range, location + descendants, checklist, trend bucket), `widget_data` (the single function used by the web widgets and the API), previous-period comparison, drill-down URLs.
* `services/layouts.py` + `DashboardLayout` - personal and organization-shared layouts (order + hidden flags only), validated on save and repaired on load.
* `templatetags/charts.py` - server-rendered SVG charts.

## Filters
Shared across a dashboard: `date_from`/`date_to` (inclusive calendar dates, server time zone, default last 90 days, maximum 366 - Phase 19 semantics), `location` (+ `include_descendants`, default on), `template`, `bucket` (`day|week|month|quarter`; automatic: <= 31 days daily, <= 180 weekly, else monthly). Each widget applies only the filters its report registers; the others are listed under **"Not filtered by"** on the widget and in the API's `not_applied` - never silently widened. Scope (the user's `report.view` locations and the module's own visibility) is applied before aggregation, independently of filters.

## Metric kinds
* **Period metrics** count records whose business date (stated per report) falls in the range.
* **"Now" metrics** (open findings, actions with the owner, awaiting verification, overdue actions, active workflows and open tasks) are the state at calculation time, whatever the date; they have no previous-period comparison and are blank when grouped by time.

## Previous-period comparison
The equivalent range immediately before (same number of days). Change % = (current - previous) / |previous| x 100, rounded to 0.1. When the previous value is 0 or unavailable, no percentage is shown ("no percentage change") - never an infinite or invented value. A change is not a cause.

## Score comparability
Scores are the **stored current final snapshots** (Phase 15), never recalculated. The **average score KPI is shown only when the scope holds a single checklist** (select a checklist, or only one is scored in scope); otherwise it says "not comparable" and the per-checklist chart is used. Averages are **unweighted means of the stored execution percentages**. Unscored executions are excluded, never zero. Grades are each checklist's own configured bands; there is no universal scale.

## Charts and accessibility
Inline SVG (no library, no client script): `role="img"`, `<title>` and a `<desc>` text summary, a data table under every chart (open for tables), zero-based axes, series distinguished by dash pattern and text labels (not color alone), colors from theme tokens (works in dark mode), values escaped, no animation. Widgets load independently over HTMX (`hx-trigger="load"`) with loading, empty, error (with "Try again") and partial ("Not filtered by") states.

## Layouts
`/dashboards/<code>/customize/`: order numbers and show/hide checkboxes (no drag-and-drop dependency); reset to default; with `dashboard.manage`, save/reset the organization default (audited: `dashboards.shared_layout_saved|reset`). Resolution: personal -> organization default -> built-in; unknown, removed or unauthorized widgets are dropped on load and new widgets appended, so a layout never grants access.

## Drill-down
Widgets link to the Phase 19 report with only the filters that report registers (plus the clicked category, e.g. a status or severity). The report page re-validates and re-authorizes. No duplicate reporting pages.

## Performance
Each widget is one or two grouped queries (plus the previous period for KPIs); a test bounds the query count of every overview widget. Nothing loads rows into memory: summaries are aggregated in PostgreSQL and bounded by `MAX_SUMMARY_GROUPS`. No new indexes were needed for the new tables beyond the layout uniqueness constraints.

## Known limitations
Live calculation only (no cache, no pre-aggregation); one organization at a time; location comparison has no roll-up of sub-locations; workflow "time in state" history is not charted (only the current state is stored); evidence analytics are not available (no scoped bulk evidence selector, as in Phase 19).

## Troubleshooting
* A widget says "Not filtered by: template": its data has no checklist field - expected.
* "Not comparable" on the average score: select one checklist.
* Empty dashboards: the user needs `dashboard.view`, `report.view` and the module view permissions; widgets without the source permission are hidden.
* A slow widget: narrow the date range or location; widgets fail independently and can be retried.

## Tests
```bash
.\venv\Scripts\python -m pytest tests/test_dashboards_kpis.py tests/test_dashboards_surfaces.py
```
