"""Class-based-view mixins that enforce RBAC through :mod:`apps.permissions.authorization`.

Status codes (the platform-wide rule, ADR 0045):

* **404** - the organization or object is outside what the user may *see* (not a member, wrong
  tenant, out of location scope).  Existence is not revealed.
* **403** - visible, but the user lacks the permission.  Rendered as the "Access restricted" page.
* Anonymous users are redirected to sign in (``LoginRequiredMixin``).

Hiding a button is never authorization: these mixins run on the server for every request.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.http import Http404

from .authorization import can_access_organization, has_permission


class OrganizationAccessMixin(LoginRequiredMixin):
    """Requires an organization the user may enter; resolves it for the view.

    The organization comes from the validated *active organization* of the session (never from
    a client-supplied id), so a forged id cannot select a tenant.  Override
    :meth:`get_organization` for views whose URL names the organization.
    """

    def get_organization(self) -> Any:
        from apps.accounts.context import get_active_organization

        return get_active_organization(self.request)

    @property
    def organization(self) -> Any:
        if not hasattr(self, "_organization"):
            self._organization = self.get_organization()
        return self._organization

    def dispatch(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if request.user.is_authenticated:
            organization = self.organization
            if organization is None or not can_access_organization(request.user, organization):
                raise Http404
        return super().dispatch(request, *args, **kwargs)


class PermissionRequiredMixin(OrganizationAccessMixin):
    """``required_permission = P.ROLE_VIEW`` - organization-level permission for the whole view."""

    required_permission: str | None = None

    def get_required_permission(self) -> str:
        if not self.required_permission:
            raise ImproperlyConfigured(f"{type(self).__name__} must set required_permission.")
        return self.required_permission

    def has_required_permission(self) -> bool:
        return has_permission(self.request.user, self.get_required_permission(), organization=self.organization)

    def dispatch(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        organization = self.organization
        if organization is None or not can_access_organization(request.user, organization):
            raise Http404
        if not self.has_required_permission():
            raise PermissionDenied
        return super(OrganizationAccessMixin, self).dispatch(request, *args, **kwargs)


class ScopedAccessMixin(PermissionRequiredMixin):
    """Object-level: the permission must hold at the *location* the object belongs to.

    Subclasses implement :meth:`get_scope_location` (default: the object itself when it is a
    location, else its ``location`` attribute).  Fetch objects through an authorized selector so
    out-of-scope objects 404 first; this adds the 403 for visible-but-forbidden.
    """

    def has_required_permission(self) -> bool:
        # Collection-level: the user must hold the permission *somewhere* in the organization.
        from .authorization import get_access

        return get_access(self.request.user, self.organization).holds(self.get_required_permission())

    def get_scope_location(self, obj: Any) -> Any:
        return obj if hasattr(obj, "organization_id") and hasattr(obj, "parent_id") else getattr(obj, "location", None)

    def check_object_permission(self, obj: Any) -> None:
        location = self.get_scope_location(obj)
        if location is None:
            allowed = has_permission(self.request.user, self.get_required_permission(), organization=self.organization)
        else:
            allowed = has_permission(self.request.user, self.get_required_permission(), location=location)
        if not allowed:
            raise PermissionDenied

    def get_object(self, queryset=None):  # noqa: ANN001, ANN201
        obj = super().get_object(queryset)
        self.check_object_permission(obj)
        return obj
