"""Permissions admin.

Roles are data.  *System* roles (the platform templates) are read-only here and cannot be
deleted; custom roles can be edited, with their permission set inline.  The registry-mirrored
``Permission`` rows are read-only: change the registry in code and run ``seed_permissions``.
"""

from __future__ import annotations

from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from apps.core.admin import ActivityLogAdminMixin

from .models import MembershipRole, Permission, Role, RolePermission, RoleScope, UserRole


class RolePermissionInline(admin.TabularInline):
    model = RolePermission
    extra = 0
    autocomplete_fields = ("permission",)

    def _locked(self, obj) -> bool:  # noqa: ANN001
        return bool(obj and obj.is_system)

    def has_add_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return not self._locked(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return not self._locked(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return not self._locked(obj) and super().has_delete_permission(request, obj)


@admin.register(Permission)
class PermissionAdmin(admin.ModelAdmin):
    list_display = ("code", "resource", "action", "label", "is_active")
    list_filter = ("resource", "is_active")
    search_fields = ("code", "label")
    readonly_fields = ("id", "code", "resource", "action", "label", "description", "is_active", "created_at", "updated_at")

    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False


@admin.register(Role)
class RoleAdmin(ActivityLogAdminMixin, SimpleHistoryAdmin):
    activity_prefix = "role"
    list_display = ("name", "slug", "organization", "is_system", "is_active")
    list_filter = ("is_active", "is_system", "organization")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("id", "created_at", "updated_at")
    inlines = [RolePermissionInline]

    def get_readonly_fields(self, request, obj=None):  # noqa: ANN001, ANN201
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None and obj.is_system:  # a platform role is changed in code, not here
            fields += ["organization", "name", "slug", "description", "is_system", "is_active"]
        elif obj is not None:
            fields += ["is_system"]
        return fields

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return not (obj is not None and obj.is_system) and super().has_delete_permission(request, obj)


@admin.register(MembershipRole)
class MembershipRoleAdmin(SimpleHistoryAdmin):
    list_display = ("membership", "role", "scope_type", "location", "is_active", "assigned_at")
    list_filter = ("is_active", "scope_type", "role")
    search_fields = ("membership__user__email", "role__name")
    autocomplete_fields = ("membership", "role", "location")
    readonly_fields = ("id", "created_at", "updated_at", "assigned_at", "assigned_by")

    def save_model(self, request, obj, form, change) -> None:  # noqa: ANN001
        if not change:
            obj.assigned_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(RoleScope)
class RoleScopeAdmin(SimpleHistoryAdmin):
    list_display = ("__str__", "scope_type", "organization", "location", "is_active")
    list_filter = ("scope_type", "is_active", "organization")
    search_fields = ("organization__name", "location__name")
    autocomplete_fields = ("organization", "location")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(UserRole)
class UserRoleAdmin(SimpleHistoryAdmin):
    list_display = ("user", "role", "scope", "is_active", "granted_at", "expires_at")
    list_filter = ("is_active", "role")
    search_fields = ("user__email", "role__name", "role__slug")
    autocomplete_fields = ("user", "role", "scope")
    readonly_fields = ("id", "created_at", "updated_at", "granted_at")