"""Organizations admin."""

from __future__ import annotations

from django.contrib import admin
from django.utils.html import format_html
from simple_history.admin import SimpleHistoryAdmin

from .models import Organization, OrganizationMembership


@admin.register(Organization)
class OrganizationAdmin(SimpleHistoryAdmin):
    list_display = ("name", "code", "status", "city", "country", "member_count", "created_at")
    list_filter = ("status", "country")
    search_fields = ("name", "code", "slug", "legal_name")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("id", "created_at", "updated_at", "logo_preview")
    list_per_page = 50

    fieldsets = (
        (None, {"fields": ("id", "name", "code", "slug", "legal_name", "description")}),
        ("Lifecycle", {"fields": ("status",)}),
        ("Branding & contact", {"fields": ("logo", "logo_preview", "favicon", "website", "email", "phone")}),
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
        ("Regional preferences", {"fields": ("timezone", "currency", "language", "date_format")}),
        ("Advanced", {"fields": ("configuration",)}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    @admin.display(description="logo")
    def logo_preview(self, obj: Organization):  # noqa: ANN201
        if not obj.logo:
            return "—"
        return format_html('<img src="{}" alt="" style="max-height:60px;border-radius:6px;">', obj.logo.url)

    @admin.display(description="members")
    def member_count(self, obj: Organization) -> int:  # noqa: ANN001
        return obj.memberships.count()


@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(SimpleHistoryAdmin):
    list_display = ("user", "organization", "is_active", "is_default", "joined_at")
    list_filter = ("is_active", "is_default", "organization")
    search_fields = ("user__email", "organization__name")
    autocomplete_fields = ("user", "organization")
    readonly_fields = ("id", "created_at", "updated_at", "joined_at")
