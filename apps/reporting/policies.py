"""Authorization for reporting.  Data scope is *not* decided here: every report reads through the owning module's scoped selector and is additionally limited to the locations
where the user holds ``report.view``.  These checks decide who may use the reporting features at all and who may see people's names, export, or share."""

from __future__ import annotations

from typing import Any

from apps.core.policies import is_active_user
from apps.permissions.authorization import get_accessible_organizations, has_permission_anywhere
from apps.permissions.registry import P

from . import registry


def holds(user: Any, code: str, organization: Any) -> bool:
    """The user holds ``code`` (any scope) in ``organization``."""
    if not is_active_user(user) or organization is None:
        return False
    return get_accessible_organizations(user, code).filter(pk=getattr(organization, "pk", organization)).exists()


def can_report(user: Any, organization: Any) -> bool:
    return holds(user, P.REPORT_VIEW, organization)


def can_export(user: Any, organization: Any) -> bool:
    return can_report(user, organization) and holds(user, P.REPORT_EXPORT, organization)


def can_see_people(user: Any, organization: Any) -> bool:
    return holds(user, P.REPORT_PEOPLE, organization)


def can_share(user: Any, organization: Any) -> bool:
    return can_report(user, organization) and holds(user, P.REPORT_SHARE, organization)


def can_use_report(user: Any, organization: Any, report: registry.Report) -> bool:
    """Offer/run ``report``: report.view in the organization and (presentation) the source module's view permission somewhere."""
    return can_report(user, organization) and (not report.source_permission or has_permission_anywhere(user, report.source_permission))


def visible_reports(user: Any, organization: Any) -> list[registry.Report]:
    if not can_report(user, organization):
        return []
    return [r for r in registry.REGISTRY.values() if r.enabled and can_use_report(user, organization, r)]
