"""Permissions admin."""

from __future__ import annotations

from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import Role, RoleScope, UserRole


@admin.register(Role)
class RoleAdmin(SimpleHistoryAdmin):
    list_display = ("name", "slug", "organization", "is_system", "is_active")
    list_filter = ("is_active", "is_system", "organization")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("id", "created_at", "updated_at")


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
