# Report exports, private artifacts and operations

## Formats
* **CSV** - UTF-8 **with a byte-order mark** (Excel reads accents), CRLF line ends, the selected columns in stable order, dates as `YYYY-MM-DD HH:MM` in the server time zone, plain numbers, `Yes`/`No`, empty cells for nulls.
* **XLSX** (openpyxl, write-only mode) - typed cells (integers, decimals, real dates with a number format, booleans), bold headers, a sheet named after the report (sanitized to Excel's rules, <= 31 chars) and a `Filters` sheet (report, version, generated time, applied filters). No formulas are written.
* **PDF** - A4 landscape table: title, generation time, organization, period/filters, row count; column headers repeat on every page; "Page x of y"; long text wraps (max three lines per cell, then `...`). Written by a small built-in generator (no HTML is rendered, no URL is fetched). **Limitation:** standard Helvetica with WinAnsi encoding, so characters outside Latin-1 (e.g. CJK, some symbols) are replaced with `?` in the PDF; CSV and XLSX keep full Unicode.

## Formula-injection policy
Any *text* cell (and header) beginning with `=`, `+`, `-`, `@`, tab or carriage return is written with a leading apostrophe in CSV and XLSX (XLSX cells are also forced to the string type). Numeric, boolean and date cells are never altered. CSV quoting alone is not relied on.

## Limits
Interactive page size <= 100; a report is at most `MAX_ROWS` (20,000) rows - larger requests fail with `too_large` instead of truncating; artifacts <= 50 MB; exports up to 2,000 rows are generated in the request, larger ones by a Phase 18 job.

## Artifacts
See `reporting-architecture.md` (private storage, authorization on every download, expiry, hash check, audit). Settings: `REPORT_ARTIFACT_ROOT` (default `<project>/private/reports`; keep it outside any served directory and back it up only if you need exports to survive), `REPORT_ARTIFACT_RETENTION_HOURS` (72), `REPORT_RUN_RETENTION_DAYS` (90).

## Background generation and schedules (Phase 18)
Large exports are queued automatically; the runner (`manage.py process_scheduled_jobs`) must be running, otherwise the run stays *queued* ("Being prepared" in Export history). To deliver a saved report on a schedule, create a Phase 18 schedule for the task **Run a saved report** with payload `{"saved_report": "<id>", "format": "xlsx"}`; the output appears in the owner's Export history. Schedule `Clean up report exports` (platform-wide, e.g. daily) to apply retention.

## Troubleshooting
* *Export stays queued*: no runner (see `/scheduling/diagnostics/`).
* *"You no longer have access"*: the requester lost `report.view`/`report.export` before generation or download.
* *"too large"*: narrow the date range or add a location filter.
* *"This saved report is no longer valid"*: the report definition changed, or the runner lacks permission for a saved column (e.g. people names) - edit and save it again.
* *A missing file*: it expired or the private folder was cleaned; generate it again.

## Commands and tests
```bash
.\venv\Scripts\python manage.py migrate
.\venv\Scripts\python manage.py seed_permissions
.\venv\Scripts\python manage.py seed_roles
.\venv\Scripts\python -m pytest tests/test_reporting_core.py tests/test_reporting_surfaces.py
```
