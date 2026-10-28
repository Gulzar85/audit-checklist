"""Report runs and exports: request, generate, retrieve, expire.

* A **run** stores metadata only (who, what, status, counts, timing, a private artifact reference) - never rows.
* **Synchronous** when the result is at most ``SYNC_EXPORT_MAX_ROWS``; otherwise a **Phase 18 job** (``reports.generate_export``) generates it with the same code.
* **Authorization is re-checked at generation and at every retrieval** with the requester's *current* permissions: a revoked permission or membership makes the artifact unreadable.
* **Idempotent:** an identical request (same user, organization, report, normalized parameters and format) returns the existing queued / running / still-valid run instead of a new one.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone

from apps.core.services import log_activity

from .. import constants as k
from .. import policies, registry
from ..errors import ReportConflict, ReportError
from ..models import ReportRun, SavedReport
from . import artifacts, execution, export


def _audit(verb: str, run: ReportRun, actor: Any, description: str, **meta: Any) -> None:
    log_activity(verb=verb, organization=run.organization, actor=actor, target=run, description=description[:400], metadata={"report": run.report_code, "format": run.format, "correlation_id": run.correlation_id, **meta})


def _hash(user: Any, organization: Any, report_code: str, params: dict[str, Any], fmt: str) -> str:
    raw = json.dumps([str(user.pk), str(organization.pk), report_code, params, fmt], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _retention() -> timedelta:
    return timedelta(hours=int(getattr(settings, "REPORT_ARTIFACT_RETENTION_HOURS", 72)))


def _usable(run: ReportRun, now: datetime) -> bool:
    return run.status in (k.RunStatus.QUEUED, k.RunStatus.RUNNING) or (run.status == k.RunStatus.SUCCEEDED and run.expires_at is not None and run.expires_at > now)


def request_export(user: Any, organization: Any, code: Any, raw: Any, fmt: Any, *, saved: SavedReport | None = None, origin: str = k.Origin.USER, force_inline: bool = False,
                   now: datetime | None = None) -> ReportRun:
    """Validate, authorize and either generate now or queue a job.  Returns the (possibly existing) run."""
    now = now or timezone.now()
    if fmt not in k.Format.values:
        raise ReportError("Choose an export format.", "unsupported_format")
    ctx, report = execution.prepare(user, organization, code, raw, now=now)
    if fmt not in report.formats:
        raise ReportError("That export format is not supported for this report.", "unsupported_format")
    if not policies.can_export(user, organization):
        raise PermissionDenied
    normalized = ctx.params.normalized()
    request_hash = _hash(user, organization, report.code, normalized, fmt)
    previous = list(ReportRun.objects.filter(request_hash=request_hash).order_by("-created_at")[:1])
    if previous and _usable(previous[0], now):
        return previous[0]
    generation = ReportRun.objects.filter(request_hash=request_hash).count()
    try:
        with transaction.atomic():
            run = ReportRun.objects.create(organization=organization, requested_by=user, origin=origin, saved_report=saved, report_code=report.code, report_version=report.version, params=normalized, format=fmt,
                                           request_hash=request_hash, idempotency_key=hashlib.sha256(f"{request_hash}|{generation}".encode()).hexdigest())
    except IntegrityError:  # a concurrent identical request won
        return ReportRun.objects.filter(request_hash=request_hash).order_by("-created_at").first()
    _audit("reports.export_requested", run, user, "Report export requested", origin=origin)
    rows = execution.count(user, organization, code, raw)
    if force_inline or rows <= k.SYNC_EXPORT_MAX_ROWS:
        generate(run, now=now)
    else:
        from apps.jobs import services as jobs

        job, _created = jobs.enqueue("reports.generate_export", organization=organization, payload={"run": str(run.pk)}, trigger="system", actor=user, idempotency_key=f"report:{run.pk}", max_attempts=3)
        ReportRun.objects.filter(pk=run.pk).update(job=job)
        run.job = job
    return run


def generate(run: ReportRun, *, now: datetime | None = None) -> ReportRun:
    """Produce the artifact for ``run`` using the requester's *current* permissions.  Safe to call again after a failure: it only proceeds from queued / running."""
    now = now or timezone.now()
    if run.status not in (k.RunStatus.QUEUED, k.RunStatus.RUNNING):
        return run
    ReportRun.objects.filter(pk=run.pk).update(status=k.RunStatus.RUNNING, started_at=now)
    started = time.monotonic()
    try:
        if not policies.can_export(run.requested_by, run.organization):
            raise ReportError("You no longer have access to export this report.", "access_revoked")
        result = execution.all_rows(run.requested_by, run.organization, run.report_code, run.params, now=now)
        data, _mime, ext = export.render(result, run.format)
        name, size, digest = artifacts.store(data, ext)
    except PermissionDenied:
        return _fail(run, "You no longer have access to this report.", started)
    except ReportError as exc:
        return _fail(run, exc.message, started)
    except OperationalError:
        ReportRun.objects.filter(pk=run.pk).update(status=k.RunStatus.QUEUED)
        raise
    except Exception:  # noqa: BLE001 - a defect: the user gets a generic message, nothing else is stored
        _fail(run, "The report could not be generated.", started)
        raise
    ReportRun.objects.filter(pk=run.pk).update(status=k.RunStatus.SUCCEEDED, row_count=result.total, finished_at=timezone.now(), duration_ms=int((time.monotonic() - started) * 1000), artifact_name=name,
                                               artifact_size=size, artifact_sha256=digest, expires_at=timezone.now() + _retention(), error_message="")
    run.refresh_from_db()
    _audit("reports.exported", run, run.requested_by, "Report export generated", rows=result.total, size=size)
    return run


def _fail(run: ReportRun, message: str, started: float) -> ReportRun:
    ReportRun.objects.filter(pk=run.pk).update(status=k.RunStatus.FAILED, finished_at=timezone.now(), duration_ms=int((time.monotonic() - started) * 1000), error_message=message[:200])
    run.refresh_from_db()
    _audit("reports.export_failed", run, run.requested_by, "Report export failed", reason=run.error_message)
    return run


def open_artifact(user: Any, run: ReportRun, *, now: datetime | None = None) -> tuple[bytes, str, str]:
    """The file of ``run`` for ``user``: only the requester, only while the run is valid, and only if their *current* rights still allow it.  Raises ``PermissionDenied`` / ``ReportConflict``."""
    now = now or timezone.now()
    if run.requested_by_id != getattr(user, "pk", None) or not policies.can_export(user, run.organization):
        raise PermissionDenied
    report = registry.get(run.report_code)
    if report is None or not policies.can_use_report(user, run.organization, report):
        raise PermissionDenied
    if run.status != k.RunStatus.SUCCEEDED:
        raise ReportConflict("This export is not ready.", "not_ready")
    if run.expires_at is None or run.expires_at <= now:
        raise ReportConflict("This export has expired. Generate it again.", "expired")
    data = artifacts.read(run.artifact_name, run.artifact_sha256)
    if data is None:
        raise ReportConflict("The file is no longer available. Generate it again.", "missing")
    _audit("reports.downloaded", run, user, "Report export downloaded")
    _writer, mime, ext = export.WRITERS[run.format]
    return data, mime, f"{run.report_code}-{timezone.localtime(run.created_at):%Y%m%d-%H%M}.{ext}"


def runs_visible_to(user: Any, organization: Any = None):  # noqa: ANN201
    """A user sees only their own runs (and only in organizations where they still hold report.view)."""
    from apps.core.policies import is_active_user
    from apps.permissions.authorization import get_accessible_organizations
    from apps.permissions.registry import P

    if not is_active_user(user):
        return ReportRun.objects.none()
    qs = ReportRun.objects.filter(requested_by=user, organization__in=get_accessible_organizations(user, P.REPORT_VIEW).values("pk")).select_related("organization", "saved_report")
    return qs.filter(organization=organization) if organization is not None else qs


def get_run(user: Any, pk: Any):  # noqa: ANN201
    from apps.core.utils import parse_uuid

    parsed = parse_uuid(pk)
    return runs_visible_to(user).filter(pk=parsed).first() if parsed else None


def cleanup(*, now: datetime | None = None, limit: int = 500) -> dict[str, int]:
    """Delete expired artifacts (the run stays as ``expired`` metadata) and run metadata older than ``REPORT_RUN_RETENTION_DAYS``."""
    now = now or timezone.now()
    removed = deleted = 0
    for run in ReportRun.objects.filter(status=k.RunStatus.SUCCEEDED, expires_at__lt=now)[: max(1, min(limit, 5000))]:
        artifacts.remove(run.artifact_name)
        ReportRun.objects.filter(pk=run.pk).update(status=k.RunStatus.EXPIRED, artifact_name="", artifact_size=None, artifact_sha256="")
        removed += 1
    cutoff = now - timedelta(days=int(getattr(settings, "REPORT_RUN_RETENTION_DAYS", 90)))
    old = list(ReportRun.objects.filter(created_at__lt=cutoff).exclude(status__in=[k.RunStatus.QUEUED, k.RunStatus.RUNNING]).values_list("pk", "artifact_name")[: max(1, min(limit, 5000))])
    for _pk, name in old:
        if name:
            artifacts.remove(name)
    if old:
        deleted = ReportRun.objects.filter(pk__in=[pk for pk, _ in old]).delete()[0]
    return {"artifacts_removed": removed, "runs_deleted": deleted}
