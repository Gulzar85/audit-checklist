"""Organizations admin.

Lifecycle changes are made with the *status actions* (which go through the service
layer, so they are logged and validated like everywhere else); ``status`` is
read-only on the form.  Organizations are never deleted from the admin: they are
deactivated, because locations, history and (later) checklist data hang off them.
"""

from __future__ import annotations

from django.contrib import admin, messages
from django.db.models import Count
from django.utils.html import format_html
from import_export.admin import ImportExportModelAdmin
from simple_history.admin import SimpleHistoryAdmin

from apps.accounts.admin_mixins import MembershipFormsetServiceMixin
from apps.core.admin import ActivityLogAdminMixin

from . import services
from .models import Organization, OrganizationMembership
from .resources import OrganizationResource


class MembersInline(admin.TabularInline):
    """Grant / revoke access from the organization page.

    A membership is the *only* thing that puts an ordinary user inside a tenant, so
    managing it next to the organization (not just from the user) is the natural place.
    """

    model = OrganizationMembership
    extra = 0
    can_delete = False  # set the status to "left" instead; the row is kept for audit
    fields = ("user", "status", "is_default", "joined_at", "left_at")
    readonly_fields = ("joined_at", "left_at")
    autocomplete_fields = ("user",)
    verbose_name = "member"
    verbose_name_plural = "members"


@admin.register(Organization)
class OrganizationAdmin(
    MembershipFormsetServiceMixin, ActivityLogAdminMixin, ImportExportModelAdmin, SimpleHistoryAdmin
):
    resource_classes = [OrganizationResource]
    activity_prefix = "organization"
    inlines = (MembersInline,)

    list_display = ("name", "code", "status", "city", "country", "member_count", "created_at")
    list_filter = ("status", "country")
    search_fields = ("name", "code", "slug", "legal_name")
    ordering = ("name",)
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("id", "status", "created_at", "updated_at", "logo_preview", "favicon_preview")
    list_per_page = 50
    actions = ("activate_selected", "deactivate_selected", "suspend_selected")

    fieldsets = (
        (None, {"fields": ("id", "name", "code", "slug", "legal_name", "description")}),
        ("Lifecycle", {"fields": ("status",), "description": "Use the actions on the list page to change status."}),
        (
            "Branding & contact",
            {
                "fields": (
                    "logo",
                    "logo_preview",
                    "favicon",
                    "favicon_preview",
                    "primary_color",
                    "website",
                    "email",
                    "phone",
                )
            },
        ),
        (
            "Address",
            {
                "fields": (
                    "address_line1",
                    "address_line2",
                    "city",
                    "state",
                    "postal_code",
                    "country",
                )
            },
        ),
        ("Regional settings", {"fields": ("timezone", "currency", "language", "date_format")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        return super().get_queryset(request).annotate(_members=Count("memberships"))

    def get_readonly_fields(self, request, obj=None):  # noqa: ANN001, ANN201
        # The code is the stable business key: editable only while creating.
        extra = ("code",) if obj is not None else ()
        return (*super().get_readonly_fields(request, obj), *extra)

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def activity_organization(self, obj):  # noqa: ANN001, ANN201
        return obj

    @admin.display(description="logo")
    def logo_preview(self, obj: Organization):  # noqa: ANN201
        if not obj or not obj.logo:
            return "—"
        return format_html('<img src="{}" alt="Logo" height="60">', obj.logo.url)

    @admin.display(description="favicon")
    def favicon_preview(self, obj: Organization):  # noqa: ANN201
        if not obj or not obj.favicon:
            return "—"
        return format_html('<img src="{}" alt="Favicon" height="32">', obj.favicon.url)

    @admin.display(description="members", ordering="_members")
    def member_count(self, obj: Organization) -> int:
        return obj._members

    def _transition(self, request, queryset, action, label: str) -> None:  # noqa: ANN001
        changed = 0
        for organization in queryset:
            if action(organization, actor=request.user).status != organization.status:
                changed += 1
        self.message_user(
            request, f"{changed} organization(s) {label}.", messages.SUCCESS
        )

    @admin.action(description="Activate selected organizations")
    def activate_selected(self, request, queryset):  # noqa: ANN001, ANN201
        self._transition(request, queryset, services.activate_organization, "activated")

    @admin.action(description="Deactivate selected organizations")
    def deactivate_selected(self, request, queryset):  # noqa: ANN001, ANN201
        self._transition(request, queryset, services.deactivate_organization, "deactivated")

    @admin.action(description="Suspend selected organizations")
    def suspend_selected(self, request, queryset):  # noqa: ANN001, ANN201
        self._transition(request, queryset, services.suspend_organization, "suspended")


@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(SimpleHistoryAdmin):
    list_display = ("user", "organization", "status", "is_default", "joined_at", "left_at")
    list_filter = ("status", "is_default", "organization")
    search_fields = ("user__email", "organization__name")
    autocomplete_fields = ("user", "organization")
    readonly_fields = ("id", "is_active", "left_at", "created_at", "updated_at", "joined_at")
    actions = ("suspend_selected", "reinstate_selected", "leave_selected")

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False  # memberships end with status "left"; the row is kept for audit

    def _transition(self, request, queryset, status, label: str) -> None:  # noqa: ANN001
        from apps.accounts import services as identity_services

        for membership in queryset:
            identity_services.set_membership_status(membership, status, actor=request.user)
        self.message_user(request, f"{queryset.count()} membership(s) {label}.", messages.SUCCESS)

    @admin.action(description="Suspend selected memberships")
    def suspend_selected(self, request, queryset):  # noqa: ANN001, ANN201
        self._transition(request, queryset, "suspended", "suspended")

    @admin.action(description="Reinstate selected memberships")
    def reinstate_selected(self, request, queryset):  # noqa: ANN001, ANN201
        self._transition(request, queryset, "active", "reinstated")

    @admin.action(description="Mark selected memberships as left")
    def leave_selected(self, request, queryset):  # noqa: ANN001, ANN201
        self._transition(request, queryset, "left", "marked as left")

    def save_model(self, request, obj, form, change) -> None:  # noqa: ANN001
        from apps.accounts import services as identity_services

        if change:
            identity_services.set_membership_status(obj, obj.status, actor=request.user)
            if form.cleaned_data.get("is_default") and not obj.is_default and obj.status == "active":
                identity_services.set_default_organization(obj.user, obj.organization, actor=request.user)
            return
        created = identity_services.add_membership(
            obj.user, obj.organization, actor=request.user, is_default=obj.is_default
        )
        if created.status != obj.status:
            identity_services.set_membership_status(created, obj.status, actor=request.user)
        obj.__dict__.update(created.__dict__)
