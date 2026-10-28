# 0087. Report runs, saved reports and background generation

- **Status:** Accepted
- **Date:** 2026-10-28

## Decision
* `ReportRun` stores **metadata only** (normalized parameters, status, counts, timing, artifact reference). Identical requests (user, organization, report, normalized parameters, format) share one run while it is queued, running or valid.
* Exports up to 2,000 rows are generated in the request; larger ones by the Phase 18 task `reports.generate_export` (payload: the run id only; the organization comes from the job row and must match). Both paths call the same `generate`, using the requester's **current** permissions.
* `SavedReport` stores configuration, never authorization: it is validated when saved and again on every run with the runner's permissions (a column or filter they may not use is an error, never silently dropped). Private or organization-shared (`report.share`); only the owner edits or deletes.
* Scheduled delivery is a Phase 18 schedule of `reports.run_saved_report` (output to the owner's export history); retention is `reports.cleanup_artifacts`. No reporting scheduler exists.

## Consequences
At-least-once background generation with idempotent requests; revoked access fails runs and downloads; no report rows are persisted.
