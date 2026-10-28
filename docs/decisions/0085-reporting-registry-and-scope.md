# 0085. Reporting: a controlled registry over the modules' scoped selectors

- **Status:** Accepted
- **Date:** 2026-10-28

## Decision
* Reports are **server-side definitions** (`apps/reporting/registry.py`): code, version, columns mapped to allowlisted ORM paths, filters mapped to allowlisted lookups, groupings, default sort, limits, formats, required source permission. User input only selects registered keys; ORM lookups, model names, expressions and SQL are never accepted. No report builder or SQL editor.
* **Scope comes first and from the owning module**: every report reads through that module's `*_visible_to` selector for the active organization and is intersected with the locations where the user holds `report.view`. Filters (including location + descendants) can only narrow. Score / evaluation columns are additionally limited to locations where the user may view scores / evaluations - shown blank, never zero. People names need `report.people`.
* Reports never recalculate or write business facts: scores are the stored current final snapshots, evaluations the stored runs, overdue the modules' lifecycle definitions.
* Dates: inclusive calendar dates in the server time zone, applied as a half-open interval; default 90 days, maximum 366.
* One organization per run (the active one). Evidence is not reported: Phase 11 has no scoped bulk selector and its authorization is not duplicated.

## Consequences
Reports are as secure as the modules they read; new reports are a definition plus tests; cross-organization reporting needs a future explicit design.
