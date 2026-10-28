# Reporting architecture (Phase 19)

A controlled, read-only reporting engine inside the Django monolith (`apps/reporting`). No SQL editor, no dynamic fields, no warehouse, no Redis/Celery.

## Repository findings (audit)
* **Existing:** `apps/reporting` was an empty Phase 0 shell; a `report.view` permission and a planned "Reports" nav item existed. Every business module already exposes a **scoped read selector** (`executions_visible_to`, `assignments_visible_to`, `findings_visible_to`, `actions_visible_to`, `instances_visible_to`, `runs_visible_to`, `results_visible_to`) and `get_accessible_locations` / `get_descendant_ids` for the self-referential location tree. Phase 15 stores score snapshots (`ScoreResult`, `is_current`, `status=final`), Phase 16 stores evaluation runs, Phase 18 provides the job registry and runner.
* **Dependencies:** `openpyxl` (added to `requirements/base.txt`) for XLSX; **no PDF library is usable on this host** - `WeasyPrint` is listed for production but fails to load its native libraries on Windows, and rendering HTML templates to PDF would also widen the attack surface. PDF is therefore produced by a small built-in writer (see exports doc). `tablib`/`import-export` were already installed (admin use only).
* **Reuse:** all scope enforcement, lifecycle definitions (overdue, terminal statuses), score and evaluation facts. **Nothing is recalculated** by reports.

## Registry (`registry.py`, `reports/definitions.py`)
A `Report` has a stable `code` and `version`, `kind` (`list` | `summary`), `columns` (each mapped to an allowlisted ORM path or a function building an expression), `filters` (each mapped to an allowlisted lookup), `group_by`, `default_sort`, `max_rows`, `formats`, the permission its source needs and a `date_field`/`date_label`. Duplicate codes, unknown default sorts, bad formats and duplicate columns raise `ImproperlyConfigured` at import. User input only *selects* registered keys (`filters.clean`): unknown filters, columns, sorts or groupings are errors; ORM lookups (`status__in`), model names and expressions are never accepted. A column may be `people=True` (needs `report.people`); score and evaluation columns are additionally limited to locations where the user holds the score / evaluation view permission (blank, never zero).

## Reports (see `docs/reporting-reports-and-metrics.md`)
executions, assignments, completion, scores, score_summary, evaluations, findings, corrective_actions, workflows, location_comparison. **Evidence: not implemented (blocked)** - Phase 11 exposes only per-execution, per-item authorized lists (`attachments_visible_to` returns a list for one target), no scoped bulk queryset; a bulk report would have to reimplement that authorization, which this phase refuses to duplicate.

## Scope and authorization
1. `report.view` in the **active organization** is required (`policies.can_report`), plus the report's source permission somewhere (catalogue visibility).
2. Rows come from the owning module's *scoped selector* for that organization and are **intersected with the locations where the user holds `report.view`** (`apply_common`) - independent of any filter. The `location` filter (optionally with descendants) can only narrow.
3. People names/filters need `report.people`; exports need `report.export`; organization-shared saved reports need `report.share`.
4. A run is for **one organization** (the active one); there is no cross-organization report (documented limitation).

## Date semantics
`date_from` / `date_to` are calendar dates in the server time zone, **both inclusive**; internally `[date_from 00:00, date_to + 1 day 00:00)`. No date = the last 90 days; the maximum range is 366 days. Each report states which timestamp the range applies to (`date_label`). Summary metrics that are states ("open findings now") say so in their column label.

## Execution lifecycle
`prepare` (exists + enabled -> authorized -> validated) -> build a `values()` queryset (database-side annotations; subqueries for one-to-many counts so joins never multiply rows) ordered by the chosen column **with the primary key as tie-break** -> count + slice (interactive pages <= 100) or iterate up to `max_rows` (exports raise `too_large` instead of truncating). Summary reports aggregate in the database (`Count(filter=...)`, `Avg`) and are bounded by `MAX_SUMMARY_GROUPS`. Reads only; a test proves no business row changes.

## Models (`models.py`, migration `0001_initial`)
* `SavedReport` - configuration only (code, version, normalized filters/columns/sort/grouping, visibility `private|organization`); unique per owner and name; simple-history.
* `ReportRun` - metadata only (requester, organization, normalized params, format, status, row count, timing, correlation id, safe error, private artifact name/size/hash, expiry, optional Phase 18 job). **No result rows are stored.**

## Runs, exports and Phase 18
`request_export` validates, authorizes (`report.export`) and either generates immediately (<= 2000 rows) or creates a queued run and a Phase 18 job `reports.generate_export` (organization-scoped; the payload is only the run id; the organization comes from the job row and must equal the run's). Both paths call the same `generate`, which uses the requester's **current** permissions (revoked access fails the run with a safe message). Identical requests (same user, organization, report, normalized parameters, format) share one run (`request_hash`; a failed or expired run can be requested again). `reports.run_saved_report` lets a Phase 18 *schedule* export a saved report for its owner (permanent failure if the owner lost access); `reports.cleanup_artifacts` is the retention task. There is no separate report scheduler.

## Private artifacts
Files live under `REPORT_ARTIFACT_ROOT` (default `<project>/private/reports`, git-ignored, never served by the web server) with random 48-hex names. The only way to a file is `open_artifact`: requester only, `report.export` + report access re-checked now, status/expiry/SHA-256 verified, audited (`reports.downloaded`); the file name is read from the database, never from a URL, and `Cache-Control: private, no-store` is set. Retention: artifacts expire after `REPORT_ARTIFACT_RETENTION_HOURS` (72); run metadata is deleted after `REPORT_RUN_RETENTION_DAYS` (90).

## Audit
`reports.export_requested`, `reports.exported`, `reports.export_failed`, `reports.downloaded`, `reports.saved_created|updated|deleted` - with report code, format, row count, correlation id; never rows or file names.

## Performance
One grouped query per metric; one subquery per derived column; scope as `IN (subquery)`; indexes on the new tables only (existing tables already index status/location/date). Interactive queries are bounded and paginated; the query-count test guards against N+1.

## Known limitations
No evidence report; no cross-organization comparison; location comparison does not roll up sub-locations; PDF is Latin-1 only (see exports doc); score/evaluation columns reflect stored snapshots; no caching (not needed at this scale).
