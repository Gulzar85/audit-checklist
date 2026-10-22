"""Django admin for assignments: a *read-only* inspection tool.

There is deliberately no add / change / delete: a write here would skip the lifecycle service (transitions, version
eligibility, duplicate policy, scope checks, audit).  Platform staff create, issue, cancel and reopen through the application
(or its API).  Non-superusers only see assignments inside their own organization/location scope.
"""

from __future__ import annotations

from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from . import selectors
from .models import ChecklistAssignment


@admin.register(ChecklistAssignment)
class ChecklistAssignmentAdmin(SimpleHistoryAdmin):
    list_display = ("reference", "organization", "version", "location", "status", "priority", "assigned_to", "due_at", "created_at")
    list_filter = ("status", "priority", "organization")
    search_fields = ("location__name", "version__template__name", "version__template__code", "instructions")
    list_select_related = ("organization", "version__template", "location", "assigned_to")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    actions = None  # no bulk delete / status actions

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(pk__in=selectors.assignments_visible_to(request.user).values("pk"))

    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False


