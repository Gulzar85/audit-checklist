"""Reusable API permission classes."""

from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsActiveUser(BasePermission):
    """Allow access only to active, authenticated users."""

    message = "This account is inactive."

    def has_permission(self, request, view) -> bool:  # noqa: ANN001
        user = request.user
        return bool(user and user.is_authenticated and user.is_active)


class PolicyPermission(BasePermission):
    """Delegate authorization to the view's *policy hooks*.

    The hooks call the same policy functions the HTML views use (``apps/*/policies.py``),
    so the API has no security model of its own:

    * ``policy_allows_collection(request)`` - list / create-style access;
    * ``policy_allows_object(request, obj)`` - access to one (already visible) object.

    A view without a hook allows that kind of access.  Objects outside the user's
    scope never reach the object hook: the queryset (a selector) 404s them first.
    """

    message = "You do not have permission to perform this action."

    def has_permission(self, request, view) -> bool:  # noqa: ANN001
        hook = getattr(view, "policy_allows_collection", None)
        return bool(hook(request)) if hook else True

    def has_object_permission(self, request, view, obj) -> bool:  # noqa: ANN001
        hook = getattr(view, "policy_allows_object", None)
        return bool(hook(request, obj)) if hook else True


class ReadOnly(BasePermission):
    """Allow only safe (read) methods."""

    def has_permission(self, request, view) -> bool:  # noqa: ANN001
        return request.method in SAFE_METHODS
