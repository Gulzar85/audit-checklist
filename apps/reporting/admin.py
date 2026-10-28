"""Django admin for reporting: read-only inspection of saved reports and run metadata (no rows are stored; artifacts are never served from the admin)."""

from __future__ import annotations

from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import ReportRun, SavedReport


class ReadOnlyMixin:
    actions = None

    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False


@admin.register(SavedReport)
class SavedReportAdmin(ReadOnlyMixin, SimpleHistoryAdmin):
    list_display = ("name", "report_code", "owner", "organization", "visibility", "updated_at")
    list_filter = ("report_code", "visibility")
    search_fields = ("name",)
    list_select_related = ("owner", "organization")
    exclude = ("filters",)

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.none()


@admin.register(ReportRun)
class ReportRunAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ("report_code", "requested_by", "organization", "format", "status", "row_count", "created_at")
    list_filter = ("report_code", "status", "format", "origin")
    list_select_related = ("requested_by", "organization")
    exclude = ("params", "artifact_name", "artifact_sha256", "request_hash", "idempotency_key")

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.none()
