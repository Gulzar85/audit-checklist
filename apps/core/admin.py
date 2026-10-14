"""Core admin registrations."""

from __future__ import annotations

from django.contrib import admin

from .models import ActivityLog


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "verb", "actor", "organization", "target_type", "target_id")
    list_filter = ("verb", "target_type")
    search_fields = ("verb", "description", "target_id")
    readonly_fields = (
        "id",
        "organization",
        "actor",
        "verb",
        "description",
        "target_type",
        "target_id",
        "metadata",
        "created_at",
    )
    date_hierarchy = "created_at"

    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False
