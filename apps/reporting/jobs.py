"""Phase 18 integration: three registered tasks.  Reporting has no scheduler of its own - a schedule runs ``reports.run_saved_report`` for a saved report and the Phase 18 runner executes it.

* ``reports.generate_export``  - generates a queued large export.  The run id is the payload; the *organization comes from the job row* and must equal the run's, and the
  requester's current permissions are re-checked by the same ``generate`` the synchronous path uses.
* ``reports.run_saved_report`` - (scheduled) exports a saved report on behalf of its owner, with the owner's current permissions; fails permanently if they lost access.
* ``reports.cleanup_artifacts`` - retention (expired files, old run metadata).
"""

from __future__ import annotations

from typing import Any

from apps.jobs import registry as jobs
from apps.jobs.errors import PermanentError
from apps.jobs.registry import Context, Field, Task

from . import constants as k
from .errors import ReportError


def generate_export(ctx: Context) -> dict[str, Any]:
    from .models import ReportRun
    from .services import runs

    run = ReportRun.objects.select_related("organization", "requested_by").filter(pk=ctx.payload["run"]).first()
    if run is None or ctx.organization is None or run.organization_id != ctx.organization.pk:
        raise PermanentError("The export request was not found.")
    done = runs.generate(run)
    if done.status == k.RunStatus.FAILED:
        raise PermanentError(done.error_message or "The export failed.")
    return {"rows": done.row_count or 0, "status": done.status}


def run_saved_report(ctx: Context) -> dict[str, Any]:
    from .models import SavedReport
    from .services import runs

    saved = SavedReport.objects.select_related("owner", "organization").filter(pk=ctx.payload["saved_report"]).first()
    if saved is None or ctx.organization is None or saved.organization_id != ctx.organization.pk:
        raise PermanentError("The saved report was not found.")
    try:
        run = runs.request_export(saved.owner, saved.organization, saved.report_code, {"filters": saved.filters, "columns": saved.columns, "sort": saved.sort, "group_by": saved.group_by},
                                  ctx.payload["format"], saved=saved, origin=k.Origin.SCHEDULE, force_inline=True)
    except (ReportError, PermissionError) as exc:
        raise PermanentError(getattr(exc, "message", "The saved report can no longer be generated.")) from exc
    except Exception as exc:  # noqa: BLE001 - PermissionDenied from the owner's lost access
        if exc.__class__.__name__ == "PermissionDenied":
            raise PermanentError("The owner no longer has access to this report.") from exc
        raise
    if run.status == k.RunStatus.FAILED:
        raise PermanentError(run.error_message or "The export failed.")
    return {"rows": run.row_count or 0, "status": run.status}


def cleanup_artifacts(ctx: Context) -> dict[str, Any]:
    from .services import runs

    return runs.cleanup(limit=ctx.payload["limit"])


jobs.register(Task("reports.generate_export", "Generate a report export", "Generates a queued large report export for its requester.", jobs.SCOPE_ORGANIZATION, generate_export,
                   {"run": Field("uuid", "", help="The report run to generate.")}, schedulable=False, manual=False, max_attempts=3, base_delay_seconds=120, soft_limit_seconds=600))
jobs.register(Task("reports.run_saved_report", "Run a saved report", "Exports a saved report for its owner (scheduled report delivery to the owner's report history).", jobs.SCOPE_ORGANIZATION, run_saved_report,
                   {"saved_report": Field("uuid", "", help="The saved report."), "format": Field("choice", "xlsx", choices=tuple(k.Format.values))}, max_attempts=2, base_delay_seconds=300, soft_limit_seconds=600))
jobs.register(Task("reports.cleanup_artifacts", "Clean up report exports", "Deletes expired export files and old report-run metadata.", jobs.SCOPE_GLOBAL, cleanup_artifacts,
                   {"limit": Field("int", 500, 1, 5000)}, max_attempts=2, base_delay_seconds=300))
