"""Authorization policy for the organization domain.

Decisions ask *"may this user do this to this object?"* and are isolated from
business operations so the HTML views, the API and the admin all share one rule
set (see ADR 0012).

Phase 1 rule set:

* **Visibility** — platform staff see everything; other users see the
  organizations they are an active member of.
* **Management** — platform staff, or an active member who additionally holds
  Django's model permission (``organizations.change_organization`` /
  ``...add_organization``).  Roles/scopes will refine this in a later phase
  without changing these call sites.
"""

from __future__ import annotations

from typing import Any

from apps.core.policies import is_platform_staff

from .models import OrganizationMembership


def _is_active_user(user: Any) -> bool:
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
    )


def is_organization_member(user: Any, organization: Any) -> bool:
    """Return ``True`` if ``user`` is an active member of ``organization``."""
    if not _is_active_user(user):
        return False
    organization_id = getattr(organization, "pk", organization)
    return OrganizationMembership.objects.filter(
        user=user,
        organization_id=organization_id,
        is_active=True,
    ).exists()


def can_view_organization(user: Any, organization: Any) -> bool:
    """Whether ``user`` may view ``organization``."""
    if not _is_active_user(user):
        return False
    if is_platform_staff(user):
        return True
    return is_organization_member(user, organization)


def can_create_organization(user: Any) -> bool:
    """Whether ``user`` may create a new organization."""
    if not _is_active_user(user):
        return False
    return is_platform_staff(user) or user.has_perm("organizations.add_organization")


def can_manage_organization(user: Any, organization: Any) -> bool:
    """Whether ``user`` may edit / (de)activate ``organization``."""
    if not _is_active_user(user):
        return False
    if is_platform_staff(user):
        return True
    return is_organization_member(user, organization) and user.has_perm(
        "organizations.change_organization"
    )
