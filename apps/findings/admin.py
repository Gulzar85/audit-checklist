"""Django admin for findings: a *read-only* inspection tool.

There is deliberately no add / change / delete and no bulk action: a write here would skip the lifecycle service (transitions,
required reasons, closure policy, assignee eligibility, scope checks, traceability, audit).  Categories and severity levels are
changed in the application (``finding.manage``) so their rules (unique rank, one default, immutable code) always apply.
Non-superusers only see findings inside their own organization / location scope.
"""

from __future__ import annotations

from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from . import selectors
from .models import Finding, FindingCategory, FindingSeverity


class ReadOnlyAdmin(SimpleHistoryAdmin):
    actions = None

    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False


@admin.register(Finding)
class FindingAdmin(ReadOnlyAdmin):
    list_display = ("reference", "title", "organization", "category", "severity", "status", "source_type", "location", "assigned_to", "due_at", "detected_at")
    list_filter = ("status", "source_type", "severity", "category", "needs_review", "organization")
    search_fields = ("title", "description", "location__name", "rule_name")
    list_select_related = ("organization", "category", "severity", "location", "assigned_to")
    ordering = ("-detected_at",)
    date_hierarchy = "detected_at"
    exclude = ("detection_key",)

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(pk__in=selectors.findings_visible_to(request.user).values("pk"))


class _ConfigAdmin(ReadOnlyAdmin):
    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        from apps.permissions.authorization import visible_organization_ids

        return qs.filter(organization_id__in=visible_organization_ids(request.user))


@admin.register(FindingCategory)
class FindingCategoryAdmin(_ConfigAdmin):
    list_display = ("name", "code", "organization", "is_active", "is_system", "sort_order")
    list_filter = ("is_active", "is_system", "organization")
    search_fields = ("name", "code")


@admin.register(FindingSeverity)
class FindingSeverityAdmin(_ConfigAdmin):
    list_display = ("name", "code", "organization", "rank", "is_default", "requires_owner", "is_escalation", "default_due_days", "is_active")
    list_filter = ("is_active", "is_default", "is_escalation", "organization")
    search_fields = ("name", "code")
