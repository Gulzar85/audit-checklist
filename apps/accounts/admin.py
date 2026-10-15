"""Accounts admin.

The lifecycle field an administrator edits is ``status``; ``is_active`` is its
stored mirror and is read-only here so the two can never be set to disagree.
Common operations (approve, suspend, reactivate, deactivate) are admin actions
that call the identity service layer, so validation, locking and activity
logging happen exactly once.
"""

from __future__ import annotations

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _
from simple_history.admin import SimpleHistoryAdmin

from apps.organizations.models import OrganizationMembership

from . import services
from .constants import UserStatus
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
    list_display = ("email", "display_name", "status", "is_staff", "last_login")
    list_filter = ("status", "is_staff", "is_superuser", "entra_tenant_id")
    search_fields = (
        "email",
        "display_name",
        "first_name",
        "last_name",
        "entra_object_id",
        "entra_tenant_id",
    )
    readonly_fields = (
        "id",
        "is_active",
        "entra_object_id",
        "entra_tenant_id",
        "last_login",
        "date_joined",
    )
    filter_horizontal = ("groups", "user_permissions")
    inlines = (MembershipInline,)
    actions = ("approve_users", "suspend_users", "deactivate_users")

    fieldsets = (
        (None, {"fields": ("id", "email", "password", "status")}),
        (
            _("Microsoft Entra ID"),
            {"fields": ("entra_object_id", "entra_tenant_id", "is_active")},
        ),
        (
            _("Profile"),
            {"fields": ("display_name", "first_name", "last_name", "job_title", "phone_number", "avatar")},
        ),
        (
            _("Permissions"),
            {
                "fields": (
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        (_("Timestamps"), {"fields": ("last_login", "date_joined")}),
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

    # --- Lifecycle actions ------------------------------------------------
    @admin.action(description=_("Approve selected users (make them active)"))
    def approve_users(self, request, queryset):  # noqa: ANN001
        self._apply_status(request, queryset, UserStatus.ACTIVE)

    @admin.action(description=_("Suspend selected users"))
    def suspend_users(self, request, queryset):  # noqa: ANN001
        self._apply_status(request, queryset, UserStatus.SUSPENDED)

    @admin.action(description=_("Deactivate selected users"))
    def deactivate_users(self, request, queryset):  # noqa: ANN001
        self._apply_status(request, queryset, UserStatus.INACTIVE)

    @staticmethod
    def _apply_status(request, queryset, status) -> None:  # noqa: ANN001
        changed = 0
        for user in queryset:
            if services.set_user_status(user, status, actor=request.user) is not None:
                changed += 1
        self.message_user(request, _(f"{changed} user(s) set to {status}."))
