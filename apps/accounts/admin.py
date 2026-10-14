"""Accounts admin."""

from __future__ import annotations

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from simple_history.admin import SimpleHistoryAdmin

from apps.organizations.models import OrganizationMembership

from .models import User


class MembershipInline(admin.TabularInline):
    model = OrganizationMembership
    extra = 0
    fields = ("organization", "is_active", "is_default", "joined_at")
    readonly_fields = ("joined_at",)
    autocomplete_fields = ("organization",)


@admin.register(User)
class UserAdmin(BaseUserAdmin, SimpleHistoryAdmin):
    ordering = ("email",)
    list_display = ("email", "display_name", "is_active", "is_staff", "last_login")
    list_filter = ("is_active", "is_staff", "is_superuser")
    search_fields = ("email", "display_name", "first_name", "last_name", "entra_object_id")
    readonly_fields = ("id", "last_login", "date_joined")
    filter_horizontal = ("groups", "user_permissions")
    inlines = (MembershipInline,)

    fieldsets = (
        (None, {"fields": ("id", "email", "password")}),
        (
            "Identity",
            {"fields": ("entra_object_id", "display_name", "first_name", "last_name")},
        ),
        ("Profile", {"fields": ("job_title", "phone_number", "avatar")}),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Timestamps", {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "display_name", "password1", "password2"),
            },
        ),
    )
