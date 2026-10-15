"""Read-only queries for the theme engine.

``themes_visible_to`` is the single scoping point (like ``organizations_visible_to``): the
web UI, the API and the admin all start from it, so an organization's themes can never leak
to another tenant.  Out of scope means *not found*; visible-but-forbidden is decided by the
policy layer.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Q, QuerySet

from apps.core.policies import is_active_user, is_platform_staff
from apps.organizations.models import Organization, OrganizationMembership
from apps.organizations.selectors import organizations_visible_to

from .constants import ThemeScope, ThemeStatus
from .models import Theme, ThemeActivation
from .tokens import diff_tokens


def themes_visible_to(user: Any) -> QuerySet[Theme]:
    """Every theme ``user`` may see.

    * anonymous / inactive users: none;
    * platform staff: every theme;
    * everyone else: system themes plus the themes of organizations they can see.  Drafts are
      work in progress, so they are visible only to people who may edit themes - and system
      drafts only to platform staff.
    """
    if not is_active_user(user):
        return Theme.objects.none()
    queryset = Theme.objects.select_related("organization")
    if is_platform_staff(user):
        return queryset
    queryset = queryset.filter(
        Q(organization__in=organizations_visible_to(user)) | Q(scope=ThemeScope.SYSTEM)
    )
    if not user.has_perm("themes.change_theme"):
        return queryset.exclude(status=ThemeStatus.DRAFT)
    return queryset.exclude(scope=ThemeScope.SYSTEM, status=ThemeStatus.DRAFT)


def get_active_theme(organization: Organization | None = None) -> Theme | None:
    """The active theme of ``organization`` (``None`` -> the system theme)."""
    queryset = Theme.objects.filter(status=ThemeStatus.ACTIVE)
    if organization is None:
        return queryset.filter(scope=ThemeScope.SYSTEM).first()
    return queryset.filter(organization=organization).first()


def get_active_themes(organization: Organization | None) -> tuple[Theme | None, Theme | None]:
    """``(system_theme, organization_theme)`` in a single query."""
    condition = Q(scope=ThemeScope.SYSTEM)
    if organization is not None:
        condition |= Q(organization=organization)
    system_theme = organization_theme = None
    for theme in Theme.objects.filter(condition, status=ThemeStatus.ACTIVE):
        if theme.scope == ThemeScope.SYSTEM:
            system_theme = theme
        else:
            organization_theme = theme
    return system_theme, organization_theme


def user_organization(user: Any) -> Organization | None:
    """The organization whose theme applies to ``user``.

    The membership marked *default*, else the earliest active membership, in an organization
    that is itself active.  A user in several organizations therefore gets one consistent
    theme (ADR 0024); platform staff without a membership get the system theme.
    """
    if not is_active_user(user):
        return None
    membership = (
        OrganizationMembership.objects.filter(
            user=user, is_active=True, organization__status=Organization.Status.ACTIVE
        )
        .select_related("organization")
        .order_by("-is_default", "joined_at")
        .first()
    )
    return membership.organization if membership else None


def previous_activation(organization: Organization | None, *, exclude_theme: Theme | None = None) -> ThemeActivation | None:
    """The most recent *ended* activation whose theme can be restored (rollback target)."""
    queryset = ThemeActivation.objects.filter(
        organization=organization,
        deactivated_at__isnull=False,
        theme__status=ThemeStatus.PUBLISHED,
    ).select_related("theme")
    if exclude_theme is not None:
        queryset = queryset.exclude(theme=exclude_theme)
    return queryset.order_by("-deactivated_at", "-activated_at").first()


def activation_history(theme: Theme, limit: int = 20) -> list[ThemeActivation]:
    """Activation periods for the scope of ``theme`` (what was active, and when)."""
    return list(
        ThemeActivation.objects.filter(organization=theme.organization)
        .select_related("theme", "activated_by")
        .order_by("-activated_at")[:limit]
    )


def theme_history(theme: Theme, limit: int = 50) -> list[dict[str, Any]]:
    """Who changed what, and when - token by token (answers "who changed the primary colour?")."""
    records = list(theme.history.select_related("history_user").order_by("-history_date")[: limit + 1])
    entries: list[dict[str, Any]] = []
    for index, record in enumerate(records[:limit]):
        older = records[index + 1] if index + 1 < len(records) else None
        entries.append(
            {
                "date": record.history_date,
                "user": record.history_user,
                "reason": record.history_change_reason or "",
                "type": {"+": "created", "~": "changed", "-": "deleted"}.get(record.history_type, ""),
                "status": record.status,
                "changes": diff_tokens(older.tokens if older else {}, record.tokens),
            }
        )
    return entries
