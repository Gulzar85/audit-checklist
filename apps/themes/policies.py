"""Authorization policy for the theme engine.

Who may do what (ADR 0023):

* **View** - exactly the themes returned by :func:`~apps.themes.selectors.themes_visible_to`.
* **Create / edit / clone / archive** - the Django permissions ``themes.add_theme`` /
  ``change_theme``, *and* visibility of the organization.  System-scope themes additionally
  require platform staff.
* **Publish** - ``themes.publish_theme``; **activate / roll back** - ``themes.activate_theme``.
  Separate permissions so "can design" and "can put live" can be granted independently.
* **Preview** - anyone who may edit or activate the theme.

Ordinary members hold none of these, so they can read the active look and set their own
appearance - nothing else.  Callers never test roles; they call these functions.
"""

from __future__ import annotations

from typing import Any

from apps.core.policies import is_active_user, is_platform_staff
from apps.organizations.models import Organization
from apps.organizations.policies import can_view_organization

from .constants import ThemeScope, ThemeStatus
from .models import Theme
from .selectors import themes_visible_to


def _in_scope(user: Any, organization: Organization | None) -> bool:
    """System scope is a platform operation; organization scope needs a visible organization."""
    if organization is None:
        return is_platform_staff(user)
    return can_view_organization(user, organization)


def can_view_theme(user: Any, theme: Theme) -> bool:
    return is_active_user(user) and themes_visible_to(user).filter(pk=theme.pk).exists()


def can_create_theme(user: Any, organization: Organization | None) -> bool:
    return (
        is_active_user(user)
        and user.has_perm("themes.add_theme")
        and _in_scope(user, organization)
    )


def can_manage_theme(user: Any, theme: Theme) -> bool:
    """Edit a draft, or create a new version / archive."""
    return (
        is_active_user(user)
        and user.has_perm("themes.change_theme")
        and _in_scope(user, theme.organization)
    )


def can_edit_theme(user: Any, theme: Theme) -> bool:
    return theme.status == ThemeStatus.DRAFT and can_manage_theme(user, theme)


def can_publish_theme(user: Any, theme: Theme) -> bool:
    return (
        theme.status == ThemeStatus.DRAFT
        and is_active_user(user)
        and user.has_perm("themes.publish_theme")
        and _in_scope(user, theme.organization)
    )


def can_activate_theme(user: Any, theme: Theme) -> bool:
    return (
        theme.status == ThemeStatus.PUBLISHED
        and is_active_user(user)
        and user.has_perm("themes.activate_theme")
        and _in_scope(user, theme.organization)
    )


def can_rollback_theme(user: Any, theme: Theme) -> bool:
    return (
        theme.status == ThemeStatus.ACTIVE
        and is_active_user(user)
        and user.has_perm("themes.activate_theme")
        and _in_scope(user, theme.organization)
    )


def can_archive_theme(user: Any, theme: Theme) -> bool:
    return theme.status in (ThemeStatus.DRAFT, ThemeStatus.PUBLISHED) and can_manage_theme(user, theme)


def can_clone_theme(user: Any, theme: Theme, target: Organization | None = ...) -> bool:  # type: ignore[assignment]
    """Cloning reads ``theme`` and creates a new draft in ``target`` (default: its own scope)."""
    if target is ...:
        target = theme.organization
    return can_view_theme(user, theme) and can_create_theme(user, target)


def can_preview_theme(user: Any, theme: Theme) -> bool:
    return can_view_theme(user, theme) and (
        user.has_perm("themes.change_theme") or user.has_perm("themes.activate_theme")
    )


def can_export_theme(user: Any, theme: Theme) -> bool:
    return can_view_theme(user, theme) and (
        theme.status != ThemeStatus.DRAFT or can_manage_theme(user, theme)
    )


def can_import_theme(user: Any, organization: Organization | None) -> bool:
    return can_create_theme(user, organization)
