"""Django admin for dashboards: read-only view of saved layouts (metrics are never stored)."""

from __future__ import annotations

from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import DashboardLayout


@admin.register(DashboardLayout)
class DashboardLayoutAdmin(SimpleHistoryAdmin):
    list_display = ("dashboard", "organization", "user", "revision", "updated_at")
    list_filter = ("dashboard",)
    list_select_related = ("organization", "user")
    actions = None

    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.none()
