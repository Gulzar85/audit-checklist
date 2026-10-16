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


class OrganizationMembershipPermission(BasePermission):
    """Layer 2 for the API: the caller must belong to (or, as platform staff, see) an organization."""

    message = "You are not a member of an organization."

    def has_permission(self, request, view) -> bool:  # noqa: ANN001
        from apps.organizations.selectors import organizations_visible_to

        user = request.user
        return bool(user and user.is_authenticated and organizations_visible_to(user).exists())


class HasApplicationPermission(BasePermission):
    """Require an RBAC permission, named by the view.

    The view sets ``required_permission = P.ROLE_VIEW`` or, per action,
    ``required_permissions = {"list": P.ROLE_VIEW, "create": P.ROLE_CREATE}``.  Actions without
    an entry are denied (default deny).  This class answers *"does the caller hold it somewhere?"*
    - the concrete organization/location is enforced by the view's authorized queryset and by
    :class:`ScopedObjectPermission`, so the API shares one decision service with the web UI.
    """

    message = "You do not have permission to perform this action."

    def has_permission(self, request, view) -> bool:  # noqa: ANN001
        from apps.permissions.authorization import has_permission_anywhere

        if not (request.user and request.user.is_authenticated):
            return False
        mapping = getattr(view, "required_permissions", None)
        code = mapping.get(getattr(view, "action", None)) if mapping else getattr(view, "required_permission", None)
        return bool(code) and has_permission_anywhere(request.user, code)


class ScopedObjectPermission(BasePermission):
    """Object-level RBAC: the permission must hold at the object's organization / location.

    The view provides ``get_permission_code(request)`` (or ``required_permission``) and, when the
    object is not itself a location, ``get_scope(obj) -> (organization, location_or_None)``.
    An object the caller cannot see never reaches here (the queryset 404s it first).
    """

    message = "You do not have permission to perform this action on this object."

    def has_object_permission(self, request, view, obj) -> bool:  # noqa: ANN001
        from apps.permissions.authorization import has_permission

        code = (
            view.get_permission_code(request) if hasattr(view, "get_permission_code")
            else getattr(view, "required_permission", None)
        )
        if not code:
            return False
        if hasattr(view, "get_scope"):
            organization, location = view.get_scope(obj)
        elif hasattr(obj, "parent_id"):
            organization, location = obj.organization_id, obj
        else:
            organization, location = getattr(obj, "organization_id", None), None
        return has_permission(request.user, code, organization=organization, location=location)