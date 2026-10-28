"""Saved report configurations: validated on save and again on every run; ownership and visibility are checked on every access.  A saved report is configuration, never a grant."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.db.models import Q

from apps.core.policies import is_active_user
from apps.core.services import log_activity
from apps.core.utils import parse_uuid

from .. import constants as k
from .. import policies, registry
from ..errors import ReportError
from ..models import SavedReport
from . import execution

EDITABLE = ("name", "description", "filters", "columns", "sort", "group_by", "default_format", "visibility")


def _audit(verb: str, saved: SavedReport, actor: Any, text: str, **meta: Any) -> None:
    log_activity(verb=verb, organization=saved.organization, actor=actor, target=saved, description=text[:400], metadata={"report": saved.report_code, **meta})


def request_of(saved: SavedReport) -> dict[str, Any]:
    return {"filters": saved.filters, "columns": saved.columns, "sort": saved.sort, "group_by": saved.group_by}


def visible_to(user: Any, organization: Any):  # noqa: ANN201
    """Own saved reports, plus the organization-shared ones - when the user may report in the organization at all."""
    if not policies.can_report(user, organization):
        return SavedReport.objects.none()
    return SavedReport.objects.filter(organization=organization).filter(Q(owner=user) | Q(visibility=k.Visibility.ORGANIZATION)).select_related("owner")


def get(user: Any, organization: Any, pk: Any) -> SavedReport | None:
    parsed = parse_uuid(pk)
    return visible_to(user, organization).filter(pk=parsed).first() if parsed else None


def _validated(user: Any, organization: Any, code: Any, data: dict[str, Any]) -> tuple[registry.Report, dict[str, Any]]:
    ctx, report = execution.prepare(user, organization, code, {"filters": data.get("filters"), "columns": data.get("columns"), "sort": data.get("sort"), "group_by": data.get("group_by")})
    normalized = ctx.params.normalized()
    return report, normalized


def validate(user: Any, organization: Any, code: Any, data: dict[str, Any]) -> dict[str, Any]:
    """Dry-run validation (used by the builder and the API): the normalized request, or an error."""
    _report, normalized = _validated(user, organization, code, data)
    return normalized


def save(user: Any, organization: Any, code: Any, data: dict[str, Any], *, saved: SavedReport | None = None) -> SavedReport:
    if not is_active_user(user):
        raise PermissionDenied
    report, normalized = _validated(user, organization, code if saved is None else saved.report_code, data)
    name = str(data.get("name") or "").strip()
    if not name or len(name) > 120:
        raise ReportError("Give the saved report a name (up to 120 characters).", "invalid_name")
    visibility = data.get("visibility") or k.Visibility.PRIVATE
    if visibility not in k.Visibility.values:
        raise ReportError("Choose who can see this saved report.", "invalid_visibility")
    if visibility == k.Visibility.ORGANIZATION and not policies.can_share(user, organization):
        raise PermissionDenied
    fmt = data.get("default_format") or ""
    if fmt and fmt not in report.formats:
        raise ReportError("That export format is not supported for this report.", "unsupported_format")
    values = {"name": name, "description": str(data.get("description") or "")[:500], "filters": normalized["filters"], "columns": normalized["columns"], "sort": normalized["sort"],
              "group_by": normalized["group_by"], "default_format": fmt, "visibility": visibility, "report_version": report.version}
    try:
        if saved is None:
            saved = SavedReport.objects.create(organization=organization, owner=user, report_code=report.code, **values)
            _audit("reports.saved_created", saved, user, f"Saved report {name} created", visibility=visibility)
        else:
            if saved.owner_id != user.pk:
                raise PermissionDenied
            was_shared = saved.visibility
            for key, value in values.items():
                setattr(saved, key, value)
            saved.save()
            _audit("reports.saved_updated", saved, user, f"Saved report {name} updated", visibility=visibility, visibility_changed=was_shared != visibility)
    except IntegrityError as exc:
        raise ReportError("You already have a saved report with that name.", "duplicate_name") from exc
    return saved


def duplicate(user: Any, organization: Any, saved: SavedReport) -> SavedReport:
    """A private copy owned by ``user`` (revalidated with *their* permissions)."""
    base = f"{saved.name} (copy)"[:120]
    name, n = base, 2
    while SavedReport.objects.filter(organization=organization, owner=user, name=name).exists():
        name, n = f"{base[:112]} {n}", n + 1
    return save(user, organization, saved.report_code, {**request_of(saved), "name": name, "description": saved.description, "default_format": saved.default_format, "visibility": k.Visibility.PRIVATE})


def delete(user: Any, saved: SavedReport) -> None:
    if saved.owner_id != user.pk:
        raise PermissionDenied
    _audit("reports.saved_deleted", saved, user, f"Saved report {saved.name} deleted")
    saved.delete()


def run(user: Any, organization: Any, saved: SavedReport, **kwargs: Any):  # noqa: ANN201
    """Run ``saved`` as ``user``: revalidated against the *current* definition and the *current user's* permissions (a column or filter they may not use is an error, never silently dropped)."""
    try:
        return execution.run(user, organization, saved.report_code, request_of(saved), **kwargs)
    except ReportError as exc:
        raise ReportError(f"This saved report is no longer valid: {exc.message}", "saved_invalid") from exc
