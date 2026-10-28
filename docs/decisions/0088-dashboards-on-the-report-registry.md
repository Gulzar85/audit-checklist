# 0088. Dashboards read the report registry; KPIs are pointers, not formulas

- **Status:** Accepted
- **Date:** 2026-10-28

## Decision
* Every dashboard number comes from a **Phase 19 report** (`execution.run`) - the KPI and widget registries only name a report, a grouping, a metric column and fixed filters. New summary reports were added *to the report registry* so the same figures are available as reports and exports. No formula language, no SQL, no second query path; web and API call the same `widget_data`.
* Filters are a shared, validated context; each widget applies only the filters its report registers and lists the others as "not applied". Scope is the report's (module selector + `report.view` locations), applied before aggregation.
* Previous period = the same-length range immediately before; no percentage for a zero or missing baseline. "Now" metrics have no comparison.
* Average score only for a single checklist (stored snapshots, unweighted mean of percentages); otherwise "not comparable".
* Live calculation, no cache and no Phase 18 refresh: only a per-process cache exists and queries are grouped and bounded; revisit with measurements.

## Consequences
One authoritative path per number; dashboards inherit report security; some widgets ignore some filters by design and say so.
