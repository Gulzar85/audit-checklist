"""Read-only queries for checklist definitions.

``templates_for_user`` is the choke point: it starts from the organizations where the user holds
``checklist.view`` (Phase 3 RBAC) and nothing else is ever reachable - search, filters and pagination
are applied on top of it.  Structure reads use a fixed number of queries whatever the checklist size.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Max, OuterRef, Prefetch, QuerySet, Subquery

from apps.core.policies import is_active_user
from apps.core.utils import parse_uuid
from apps.permissions.authorization import get_access, visible_organization_ids
from apps.permissions.registry import P

from .constants import VersionStatus
from .models import ChecklistCategory, ChecklistItem, ChecklistOption, ChecklistSection, ChecklistTemplate, ChecklistVersion


def organization_ids_with(user: Any, code: str) -> list[Any]:
    """Organizations where ``user`` holds ``code`` organization-wide (definitions are org-level)."""
    if not is_active_user(user):
        return []
    return [pk for pk in visible_organization_ids(user) if get_access(user, pk).is_org_wide(code)]


def templates_for_user(user: Any, organization: Any = None) -> QuerySet[ChecklistTemplate]:
    """Templates the user may view, annotated with the latest version number and the draft version's id."""
    org_ids = organization_ids_with(user, P.CHECKLIST_VIEW)
    if organization is not None:
        wanted = str(getattr(organization, "pk", organization))
        org_ids = [pk for pk in org_ids if str(pk) == wanted]
    latest = ChecklistVersion.objects.filter(template=OuterRef("pk")).order_by("-version_number")
    published = ChecklistVersion.objects.filter(template=OuterRef("pk"), status=VersionStatus.PUBLISHED).order_by("-version_number")
    return (
        ChecklistTemplate.objects.filter(organization_id__in=org_ids)
        .select_related("organization", "category", "created_by", "updated_by")
        .annotate(
            latest_version_number=Subquery(latest.values("version_number")[:1]),
            published_version_number=Subquery(published.values("version_number")[:1]),
        )
    )


def get_template(user: Any, pk: Any) -> ChecklistTemplate | None:
    """One visible template or ``None`` (the view turns that into 404)."""
    pk = parse_uuid(pk)
    return templates_for_user(user).filter(pk=pk).first() if pk else None


def categories_for_user(user: Any) -> QuerySet[ChecklistCategory]:
    return ChecklistCategory.objects.filter(organization_id__in=organization_ids_with(user, P.CHECKLIST_VIEW))


def versions_of(template: ChecklistTemplate) -> QuerySet[ChecklistVersion]:
    return template.versions.select_related("created_by").order_by("-version_number")


def get_draft_version(template: ChecklistTemplate) -> ChecklistVersion | None:
    return template.versions.filter(status=VersionStatus.DRAFT).first()


def get_version(template: ChecklistTemplate, number: int) -> ChecklistVersion | None:
    return template.versions.filter(version_number=number).first()


def get_sections_for_version(version: ChecklistVersion) -> QuerySet[ChecklistSection]:
    """Sections with their items and each item's options: three queries in total."""
    items = Prefetch(
        "items",
        queryset=ChecklistItem.objects.order_by("sort_order").prefetch_related(
            Prefetch("options", queryset=ChecklistOption.objects.order_by("sort_order"))
        ),
    )
    return version.sections.order_by("sort_order").prefetch_related(items)


def get_items_for_section(section: ChecklistSection) -> QuerySet[ChecklistItem]:
    return section.items.order_by("sort_order").prefetch_related("options")


def get_section(version: ChecklistVersion, pk: Any) -> ChecklistSection | None:
    pk = parse_uuid(pk)
    return version.sections.filter(pk=pk).first() if pk else None


def get_item(version: ChecklistVersion, pk: Any) -> ChecklistItem | None:
    pk = parse_uuid(pk)
    return ChecklistItem.objects.filter(section__version=version, pk=pk).select_related("section").first() if pk else None


def structure_counts(version: ChecklistVersion) -> dict[str, int]:
    return {
        "sections": version.sections.count(),
        "items": ChecklistItem.objects.filter(section__version=version).count(),
    }


def next_code_hint(organization: Any) -> str:
    """A suggested template code (``CL-001`` ...) not yet used in the organization."""
    used = set(ChecklistTemplate.objects.filter(organization=organization).values_list("code", flat=True))
    n = len(used) + 1
    while f"CL-{n:03d}" in used:
        n += 1
    return f"CL-{n:03d}"


def latest_version_number(template: ChecklistTemplate) -> int:
    return template.versions.aggregate(top=Max("version_number"))["top"] or 0


def template_activity(template: ChecklistTemplate, limit: int = 30):  # noqa: ANN201
    """Activity-log events about the template or anything inside it (see ``services._log``)."""
    from apps.core.models import ActivityLog

    return (
        ActivityLog.objects.filter(organization_id=template.organization_id, metadata__template_id=str(template.pk))
        .select_related("actor")
        .order_by("-created_at")[:limit]
    )


def get_rule(version: ChecklistVersion, pk: Any) -> Any:
    from .models import ChecklistRule

    pk = parse_uuid(pk)
    return ChecklistRule.objects.filter(version=version, pk=pk).first() if pk else None


def rules_for_version(version: ChecklistVersion):  # noqa: ANN201
    return version.rules.order_by("sort_order")
