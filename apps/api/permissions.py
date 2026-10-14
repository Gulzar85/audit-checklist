"""Reusable API permission classes."""

from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsActiveUser(BasePermission):
    """Allow access only to active, authenticated users."""

    message = "This account is inactive."

    def has_permission(self, request, view) -> bool:  # noqa: ANN001
        user = request.user
        return bool(user and user.is_authenticated and user.is_active)


class ReadOnly(BasePermission):
    """Allow only safe (read) methods."""

    def has_permission(self, request, view) -> bool:  # noqa: ANN001
        return request.method in SAFE_METHODS
