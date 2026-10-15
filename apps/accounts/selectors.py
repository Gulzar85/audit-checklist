"""Read-only query helpers for the identity domain.

Like every selector in the platform these have no side effects and are the single
source of truth for read scoping: the HTML views, the API and the admin all start
from here, so there is no separate "API security model" (ADR 0012).
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import QuerySet

from apps.core.policies import is_active_user, is_platform_staff
from apps.organizations.models import Organization, OrganizationMembership
from apps.organizations.selectors import organizations_visible_to

User = get_user_model()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def get_user_by_entra_identity(object_id: str, tenant_id: str | None = None) -> Any | None:
    """Return the user linked to an Entra object id, if any.

    ``entra_object_id`` is globally unique, so ``tenant_id`` is only an extra
    guard: it prevents a (mis-configured) cross-tenant object-id collision from
    ever resolving to another directory's user.
    """
    if not object_id:
        return None
    queryset = User.objects.filter(entra_object_id=object_id)
    if tenant_id:
        queryset = queryset.filter(entra_tenant_id=tenant_id)
    return queryset.first()


def get_user_by_email(email: str) -> Any | None:
    """Return a user by email (case-insensitive).  Used only by linking policy."""
    if not email:
        return None
    return User.objects.filter(email__iexact=email.strip()).first()


def users_visible_to(user: Any) -> QuerySet:
    """Return the users ``user`` may see.

    * Anonymous / inactive users see nobody.
    * Platform staff see every user.
    * Everyone else sees only users who share an *active membership* in an
      organization the caller can themselves see.  This is what keeps user
      search from leaking other tenants' people (spec §37).
    """
    if not is_active_user(user):
        return User.objects.none()
    if is_platform_staff(user):
        return User.objects.all()
    org_ids = organizations_visible_to(user).values_list("pk", flat=True)
    return User.objects.filter(
        organization_memberships__organization_id__in=org_ids,
        organization_memberships__is_active=True,
    ).distinct()


def get_users_for_organization(organization: Any) -> QuerySet:
    """Return the active members of ``organization``."""
    return User.objects.filter(
        organization_memberships__organization_id=getattr(organization, "pk", organization),
        organization_memberships__is_active=True,
    ).distinct()


# ---------------------------------------------------------------------------
# Memberships
# ---------------------------------------------------------------------------
def get_memberships(user: Any) -> QuerySet[OrganizationMembership]:
    """Every membership of ``user``, ordered for display."""
    return (
        OrganizationMembership.objects.filter(user=user)
        .select_related("organization")
        .order_by("organization__name")
    )


def get_active_memberships(user: Any) -> QuerySet[OrganizationMembership]:
    """The memberships of ``user`` that are themselves active."""
    return get_memberships(user).filter(is_active=True)


def get_membership(user: Any, organization: Any) -> OrganizationMembership | None:
    """Return ``user``'s membership of ``organization``, if any."""
    return OrganizationMembership.objects.filter(
        user=user,
        organization_id=getattr(organization, "pk", organization),
    ).first()


def get_user_organizations(user: Any) -> QuerySet[Organization]:
    """The organizations ``user`` may see (delegates to the org selector)."""
    return organizations_visible_to(user)


# ---------------------------------------------------------------------------
# Active-organization resolution (request context)
# ---------------------------------------------------------------------------
def resolve_active_organization(user: Any, organization_id: str | None = None) -> Organization | None:
    """Resolve the organization the request should act within.

    The stored/requested id is **never trusted**: it is validated against the
    organizations the user may actually see.  When it is absent, stale, or points
    outside the user's scope, the user's *default* membership wins, falling back
    to the first visible organization.  Returns ``None`` for users with no
    organization at all.

    Resolved in at most one query so it can run on every (HTML) request.
    """
    if not is_active_user(user):
        return None

    visible = organizations_visible_to(user).order_by("name")
    if organization_id:
        try:
            organization = visible.filter(pk=organization_id).first()
        except (ValidationError, ValueError, TypeError):
            organization = None
        if organization is not None:
            return organization

    from django.db.models import Case, IntegerField, Max, When

    return (
        visible.filter(memberships__user=user, memberships__is_active=True)
        .annotate(
            _default=Max(
                Case(
                    When(memberships__is_default=True, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            )
        )
        .order_by("-_default", "name")
        .first()
    ) or visible.first()



def get_preferences(user: Any) -> dict[str, Any]:
    """The user's stored preferences as ``{key: value}`` (appearance is the theme engine's)."""
    from .models import UserPreference

    return dict(UserPreference.objects.filter(user=user).values_list("key", "value"))
